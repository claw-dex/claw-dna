#!/usr/bin/env python3
"""
skill_manage.py — Skill lifecycle CLI for the claw-dna learning loop.

Maintains `skills/.usage.json` (lifecycle metadata sidecar) and the
on-disk `skills/<name>/` layout. The agent invokes this to create,
patch, edit, delete, or attribute use to skills. `bump-usage` parses
a cycle transcript and increments per-skill use counters.

Subcommands:
    init                       Seed .usage.json from existing skills/*/SKILL.md
    create   --name N --description D [--body-file F]
    patch    --name N --section H --body-file F
    edit     --name N --body-file F
    write-file --name N --rel-path P --body-file F
    delete   --name N (--absorbed-into M | --prune)
    use      --name N
    touch    --name N (--pin | --unpin)
    list     [--state active|stale|archived]
    bump-usage --cycle N [--transcript PATH]

Only skills with created_by == "agent" and pinned == false are subject
to lifecycle transitions. Seed skills (imported by `init`) are pinned
by default so they are immune to auto-archive.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILLS_DIR = Path("/agent/skills")
ARCHIVE_DIR = SKILLS_DIR / ".archive"
USAGE_FILE = SKILLS_DIR / ".usage.json"
TRANSCRIPTS_DIR = Path("/agent/memory/transcripts")
NUDGES_PATH = Path("/agent/memory/nudges.json")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def write_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if isinstance(data, (dict, list)):
            tmp.write_text(json.dumps(data, indent=2))
        else:
            tmp.write_text(str(data))
        tmp.rename(path)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        die(f"Failed to write {path}: {e}")


def load_usage() -> dict:
    if not USAGE_FILE.exists():
        return {"version": 1, "skills": {}, "last_scanned_cycle": 0}
    try:
        data = json.loads(USAGE_FILE.read_text())
    except Exception as e:
        die(f"Failed to read {USAGE_FILE}: {e}")
    data.setdefault("version", 1)
    data.setdefault("skills", {})
    data.setdefault("last_scanned_cycle", 0)
    return data


def save_usage(data: dict) -> None:
    write_atomic(USAGE_FILE, data)


def validate_name(name: str) -> None:
    if not NAME_RE.match(name or ""):
        die(
            f"invalid skill name: {name!r} (lowercase letters/digits/hyphens, ≤64 chars)"
        )


def read_frontmatter_name(skill_md: Path) -> str:
    """Return the `name:` field from a SKILL.md frontmatter, or the dir name."""
    fallback = skill_md.parent.name
    if not skill_md.exists():
        return fallback
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return fallback
    m = FRONTMATTER_RE.match(text)
    if not m:
        return fallback
    nm = FRONTMATTER_NAME_RE.search(m.group(1))
    return nm.group(1).strip() if nm else fallback


def render_skill_md(name: str, description: str, body: str) -> str:
    desc = description.replace("\n", " ").strip()
    body = (body or "").rstrip() + "\n"
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


def _new_entry(*, created_by: str, pinned: bool, source: str) -> dict:
    return {
        "created_by": created_by,
        "created_at": now_iso(),
        "use_count": 0,
        "last_used_at": None,
        "patch_count": 0,
        "last_patched_at": None,
        "state": "active",
        "pinned": pinned,
        "absorbed_into": None,
        "source": source,
    }


# ── Subcommands ──────────────────────────────────────────────────────────────


def cmd_init(args) -> int:
    """Seed .usage.json from existing skills/*/SKILL.md as `created_by=seed`,
    `pinned=true`. Idempotent: existing entries are preserved untouched."""
    usage = load_usage()
    skills = usage["skills"]
    added = 0
    for sub in sorted(SKILLS_DIR.iterdir() if SKILLS_DIR.is_dir() else []):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        skill_md = sub / "SKILL.md"
        if not skill_md.exists():
            continue
        if sub.name in skills:
            continue
        skills[sub.name] = _new_entry(
            created_by="seed", pinned=True, source="bootstrap-import"
        )
        added += 1
    save_usage(usage)
    print(f"init: seeded {added} skill(s); sidecar now tracks {len(skills)}")
    return 0


def cmd_create(args) -> int:
    validate_name(args.name)
    target = SKILLS_DIR / args.name
    skill_md = target / "SKILL.md"
    if skill_md.exists():
        die(f"skill already exists: {args.name}")
    target.mkdir(parents=True, exist_ok=True)
    body = ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    skill_md.write_text(render_skill_md(args.name, args.description, body))
    usage = load_usage()
    usage["skills"][args.name] = _new_entry(
        created_by="agent", pinned=False, source="skill_manage"
    )
    save_usage(usage)
    _reset_nudge_counter("cycles_since_skill_create")
    _reset_nudge_counter("cycles_since_skill_review")
    print(f"create: {args.name} → {skill_md}")
    return 0


def cmd_patch(args) -> int:
    validate_name(args.name)
    skill_md = SKILLS_DIR / args.name / "SKILL.md"
    if not skill_md.exists():
        die(f"skill not found: {args.name}")
    addition = Path(args.body_file).read_text(encoding="utf-8").rstrip() + "\n"
    section = args.section.strip()
    existing = skill_md.read_text(encoding="utf-8")
    sep = "\n\n" if not existing.endswith("\n") else "\n"
    new_text = existing.rstrip() + sep + f"\n## {section}\n\n{addition}"
    skill_md.write_text(new_text)
    _bump_patch(args.name)
    print(f"patch: {args.name} += '## {section}'")
    return 0


def cmd_edit(args) -> int:
    """Full replacement of SKILL.md body (frontmatter preserved)."""
    validate_name(args.name)
    skill_md = SKILLS_DIR / args.name / "SKILL.md"
    if not skill_md.exists():
        die(f"skill not found: {args.name}")
    text = skill_md.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    fm = m.group(0) if m else f"---\nname: {args.name}\ndescription: \n---\n\n"
    new_body = Path(args.body_file).read_text(encoding="utf-8").rstrip() + "\n"
    skill_md.write_text(fm + new_body)
    _bump_patch(args.name)
    print(f"edit: {args.name}")
    return 0


def cmd_write_file(args) -> int:
    validate_name(args.name)
    base = SKILLS_DIR / args.name
    if not base.is_dir():
        die(f"skill not found: {args.name}")
    rel = Path(args.rel_path)
    if rel.is_absolute() or ".." in rel.parts:
        die(f"--rel-path must be relative and inside the skill dir: {rel}")
    dest = base / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = Path(args.body_file).read_text(encoding="utf-8")
    dest.write_text(content)
    _bump_patch(args.name)
    print(f"write-file: {args.name}:{rel}")
    return 0


def cmd_delete(args) -> int:
    validate_name(args.name)
    if not args.absorbed_into and not args.prune:
        die("delete requires --absorbed-into <skill> or --prune")
    if args.absorbed_into and args.prune:
        die("--absorbed-into and --prune are mutually exclusive")
    src = SKILLS_DIR / args.name
    if not src.exists():
        die(f"skill not found: {args.name}")
    usage = load_usage()
    entry = usage["skills"].get(args.name)
    if entry and entry.get("pinned") and not args.force:
        die(f"refusing to delete pinned skill {args.name} (pass --force to override)")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    dest = ARCHIVE_DIR / f"{args.name}-{stamp}"
    if dest.exists():
        dest = ARCHIVE_DIR / f"{args.name}-{stamp}-{os.getpid()}"
    shutil.move(str(src), str(dest))
    if entry is None:
        entry = _new_entry(created_by="agent", pinned=False, source="skill_manage")
    entry["state"] = "archived"
    entry["absorbed_into"] = args.absorbed_into or None
    entry["archived_at"] = now_iso()
    usage["skills"][args.name] = entry
    save_usage(usage)
    kind = f"absorbed into {args.absorbed_into}" if args.absorbed_into else "pruned"
    print(f"delete: {args.name} → {dest} ({kind})")
    return 0


def cmd_use(args) -> int:
    validate_name(args.name)
    usage = load_usage()
    entry = usage["skills"].get(args.name)
    if entry is None:
        die(f"unknown skill: {args.name}")
    entry["use_count"] = int(entry.get("use_count", 0)) + 1
    entry["last_used_at"] = now_iso()
    if entry.get("state") == "stale":
        entry["state"] = "active"
    save_usage(usage)
    print(f"use: {args.name} (count={entry['use_count']})")
    return 0


def cmd_touch(args) -> int:
    validate_name(args.name)
    if not args.pin and not args.unpin:
        die("touch requires --pin or --unpin")
    usage = load_usage()
    entry = usage["skills"].get(args.name)
    if entry is None:
        die(f"unknown skill: {args.name}")
    entry["pinned"] = bool(args.pin)
    save_usage(usage)
    print(f"touch: {args.name} pinned={entry['pinned']}")
    return 0


def cmd_list(args) -> int:
    usage = load_usage()
    rows = []
    for name, entry in sorted(usage["skills"].items()):
        if args.state and entry.get("state") != args.state:
            continue
        rows.append(
            (
                name,
                entry.get("state", "?"),
                entry.get("created_by", "?"),
                "Y" if entry.get("pinned") else "n",
                int(entry.get("use_count", 0)),
                entry.get("last_used_at") or "-",
            )
        )
    if not rows:
        print("(no matching skills)")
        return 0
    widths = [
        max(
            len(str(r[i]))
            for r in rows + [("name", "state", "by", "pin", "uses", "last_used")]
        )
        for i in range(6)
    ]
    header = ("name", "state", "by", "pin", "uses", "last_used")
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    print(fmt.format(*header))
    print(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print(fmt.format(*[str(x) for x in r]))
    return 0


# ── bump-usage (transcript parsing) ──────────────────────────────────────────


def _iter_transcript_blocks(path: Path):
    """Yield (kind, text) for each searchable text payload in a cycle JSONL.

    kind ∈ {tool_use_input, tool_result, text}. Reuses transcript_filter's
    line iterator to skip malformed JSONL lines gracefully.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from transcript_filter import iter_entries  # type: ignore
    except Exception as e:
        die(f"cannot import transcript_filter.iter_entries: {e}")

    for obj in iter_entries(path):  # type: ignore[name-defined]
        if obj.get("isSidechain") is True:
            continue
        otype = obj.get("type")
        if otype not in ("user", "assistant"):
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            yield ("text", content)
            continue
        if not isinstance(content, list):
            continue
        for blk in content:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                yield ("text", str(blk.get("text", "")))
            elif btype == "tool_use":
                try:
                    rendered = json.dumps(blk.get("input", {}), ensure_ascii=False)
                except (TypeError, ValueError):
                    rendered = str(blk.get("input", ""))
                name = str(blk.get("name", ""))
                yield ("tool_use_input", f"{name} {rendered}")
            elif btype == "tool_result":
                tr = blk.get("content", "")
                if isinstance(tr, str):
                    yield ("tool_result", tr)
                elif isinstance(tr, list):
                    parts = []
                    for sub in tr:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(str(sub.get("text", "")))
                    yield ("tool_result", "\n".join(parts))


# Detect `skill_manage.py use --name <X>` invocations in Bash tool inputs.
_USE_INVOCATION_RE = re.compile(r"skill_manage(?:\.py)?\s+use\s+--name\s+([a-z0-9-]+)")
# Detect `Skill(skill="<name>")` / `"skill": "<name>"` style harness invocations.
_SKILL_TOOL_RE = re.compile(r'(?:skill|name)["\']?\s*[:=]\s*["\']([a-z0-9-]+)["\']')


def _collect_skill_names() -> dict:
    """Return {name -> list of compiled regex matchers} for every tracked skill.

    Only directory-name-based tokens are used (the dir name is unique and
    typically distinctive). Frontmatter `name:` values are intentionally
    NOT harvested — they are often short common words (e.g. "review",
    "init", "run") that would match millions of unrelated transcript lines.
    Matching is word-boundary regex to further reduce false positives.
    """
    out: dict[str, list[re.Pattern]] = {}
    if not SKILLS_DIR.is_dir():
        return out
    for sub in SKILLS_DIR.iterdir():
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        skill_md = sub / "SKILL.md"
        if not skill_md.exists():
            continue
        name = sub.name
        patterns = [
            re.compile(rf"skills/{re.escape(name)}/", re.IGNORECASE),
            re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE),
        ]
        out[name] = patterns
    return out


def cmd_bump_usage(args) -> int:
    cycle_n = int(args.cycle)
    transcript = (
        Path(args.transcript)
        if args.transcript
        else TRANSCRIPTS_DIR / f"cycle-{cycle_n}.jsonl"
    )
    if not transcript.exists():
        # Non-fatal: emit a note and exit 0 so background invocation never
        # destabilizes cycle_close.
        print(f"bump-usage: transcript missing ({transcript}); skipping")
        return 0
    usage = load_usage()
    if int(usage.get("last_scanned_cycle") or 0) >= cycle_n:
        print(f"bump-usage: cycle {cycle_n} already scanned; skipping")
        return 0

    skill_tokens = _collect_skill_names()
    if not skill_tokens:
        usage["last_scanned_cycle"] = cycle_n
        save_usage(usage)
        print("bump-usage: no skills to track")
        return 0

    hits: set[str] = set()
    for kind, text in _iter_transcript_blocks(transcript):
        if not text:
            continue
        # Explicit self-report patterns — accept any name they reference;
        # untracked names are still skipped below.
        if kind == "tool_use_input":
            for m in _USE_INVOCATION_RE.finditer(text):
                hits.add(m.group(1))
            for m in _SKILL_TOOL_RE.finditer(text):
                hits.add(m.group(1))
        # Regex (word-boundary) scan against directory-name tokens only.
        for name, patterns in skill_tokens.items():
            if name in hits:
                continue
            for pat in patterns:
                if pat.search(text):
                    hits.add(name)
                    break

    now = now_iso()
    bumped = 0
    restored = 0
    skipped_unknown = 0
    for name in hits:
        entry = usage["skills"].get(name)
        if entry is None:
            # Don't auto-create — bumping an untracked skill would resurrect
            # phantoms. Run `skill_manage init` to seed.
            skipped_unknown += 1
            continue
        entry["use_count"] = int(entry.get("use_count", 0)) + 1
        entry["last_used_at"] = now
        if entry.get("state") == "stale":
            entry["state"] = "active"
            restored += 1
        bumped += 1
    usage["last_scanned_cycle"] = cycle_n
    save_usage(usage)
    extra = f", skipped {skipped_unknown} unknown" if skipped_unknown else ""
    print(f"bump-usage: cycle {cycle_n} → {bumped} used, {restored} restored{extra}")
    return 0


# ── helpers ──────────────────────────────────────────────────────────────────


def _mutate_nudges(mutator) -> None:
    """Read-modify-write `/agent/memory/nudges.json` under an exclusive flock.

    `mutator(data: dict) -> None` is called with the parsed dict and may mutate
    it in place. All errors are swallowed: nudges.json is non-essential, and a
    counter glitch must never crash a successful create/patch or cycle-close.
    """
    try:
        import fcntl  # POSIX-only; container is Linux.

        NUDGES_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock_path = NUDGES_PATH.with_suffix(NUDGES_PATH.suffix + ".lock")
        with open(lock_path, "a+") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                data = (
                    json.loads(NUDGES_PATH.read_text()) if NUDGES_PATH.exists() else {}
                )
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            mutator(data)
            tmp = NUDGES_PATH.with_suffix(NUDGES_PATH.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(NUDGES_PATH)
    except Exception:
        pass


def _reset_nudge_counter(field: str) -> None:
    """Reset one counter to 0. Wraps `_mutate_nudges` for backwards compat."""

    def _do(data):
        data[field] = 0

    _mutate_nudges(_do)


def _bump_patch(name: str) -> None:
    usage = load_usage()
    entry = usage["skills"].setdefault(
        name, _new_entry(created_by="agent", pinned=False, source="skill_manage")
    )
    entry["patch_count"] = int(entry.get("patch_count", 0)) + 1
    entry["last_patched_at"] = now_iso()
    save_usage(usage)
    _reset_nudge_counter("cycles_since_skill_review")


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skill_manage", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Seed .usage.json from existing skills/")

    c = sub.add_parser("create", help="Create a new skill")
    c.add_argument("--name", required=True)
    c.add_argument("--description", required=True)
    c.add_argument("--body-file")

    c = sub.add_parser("patch", help="Append a section to SKILL.md")
    c.add_argument("--name", required=True)
    c.add_argument("--section", required=True)
    c.add_argument("--body-file", required=True)

    c = sub.add_parser("edit", help="Replace SKILL.md body (frontmatter kept)")
    c.add_argument("--name", required=True)
    c.add_argument("--body-file", required=True)

    c = sub.add_parser(
        "write-file", help="Write an auxiliary file under skills/<name>/"
    )
    c.add_argument("--name", required=True)
    c.add_argument("--rel-path", required=True)
    c.add_argument("--body-file", required=True)

    c = sub.add_parser(
        "delete", help="Archive a skill (declare consolidation or prune)"
    )
    c.add_argument("--name", required=True)
    c.add_argument("--absorbed-into")
    c.add_argument("--prune", action="store_true")
    c.add_argument("--force", action="store_true", help="Allow deleting pinned skills")

    c = sub.add_parser("use", help="Manually attribute a use to a skill")
    c.add_argument("--name", required=True)

    c = sub.add_parser("touch", help="Toggle pinned flag on a skill")
    c.add_argument("--name", required=True)
    c.add_argument("--pin", action="store_true")
    c.add_argument("--unpin", action="store_true")

    c = sub.add_parser("list", help="List tracked skills")
    c.add_argument("--state", choices=("active", "stale", "archived"))

    c = sub.add_parser(
        "bump-usage", help="Parse a cycle transcript and bump use counts"
    )
    c.add_argument("--cycle", required=True, type=int)
    c.add_argument("--transcript")

    return p


HANDLERS = {
    "init": cmd_init,
    "create": cmd_create,
    "patch": cmd_patch,
    "edit": cmd_edit,
    "write-file": cmd_write_file,
    "delete": cmd_delete,
    "use": cmd_use,
    "touch": cmd_touch,
    "list": cmd_list,
    "bump-usage": cmd_bump_usage,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return HANDLERS[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

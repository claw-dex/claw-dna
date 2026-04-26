#!/usr/bin/env python3
"""
cycle_start.py — Single-command cycle startup briefing (replaces running memory-stats + memory-repair separately).

Combines in one Python process (one `uv run` invocation):
  1. Memory repair scan (detect + fix corrupted JSON)
  2. State / goals / cycles / failures summary
  3. Last 5 journal entries (so agent knows recent activity)
  4. Inbox peek (any pending messages?)
  5. Evolve category recommendation

Usage:
    uv run python scripts/cycle_start.py                    # full briefing
    uv run python scripts/cycle_start.py --short            # one-liner summary
    uv run python scripts/cycle_start.py --json             # machine-readable JSON
    uv run python scripts/cycle_start.py --no-repair        # skip memory repair step
    uv run python scripts/cycle_start.py --mode evolve      # include evolve recommendation section
    uv run python scripts/cycle_start.py --clear-old-errors # force-purge resolved tab errors

Exit codes: 0 = healthy, 1 = memory issues found (check output).

Enum Reference: See prompts/enum.md for agent status values and other enums.

Added in cycle 14 (efficiency): replaces two separate uv run invocations at cycle start.
Enhanced in cycle 129 (efficiency): inlined journal-archive logic — saves ~1.5s uv-run startup when auto-archive triggers (every ~5 cycles).
"""

import json
import os
import sys
import datetime
from pathlib import Path
from collections import Counter

MEMORY = Path("/agent/memory")
MESSAGES = Path("/agent/messages")
MV2_PATH = MEMORY / "long_term_memory.mv2"
SCRIPTS = Path("/agent/scripts")
DREAM_DIR = MEMORY / "dream"

from scripts.memory_repair import run_repair as _run_memory_repair  # noqa: E402

# ── Flags ───────────────────────────────────────────────────────────────────────
args = sys.argv[1:]
SHORT = "--short" in args
JSON_MODE = "--json" in args
NO_REPAIR = "--no-repair" in args
CLEAR_OLD_ERRORS = "--clear-old-errors" in args
CLEAR_ALL_ERRORS = (
    "--clear-all-errors" in args
)  # purge ALL errors (use when fix is confirmed)
EVOLVE_MODE = "--mode" in args and args[
    args.index("--mode") + 1 : args.index("--mode") + 2
] == ["evolve"]

# ── Helpers ─────────────────────────────────────────────────────────────────────


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def ago(ts_str: str) -> str:
    try:
        ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta = datetime.datetime.now(datetime.timezone.utc) - ts
        s = int(delta.total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s//60}m ago"
        if s < 86400:
            return f"{s//3600}h ago"
        return f"{s//86400}d ago"
    except Exception:
        return ts_str


def fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m {s%60}s"
    return f"{s//3600}h {(s%3600)//60}m"


def portal_health() -> str:
    try:
        import urllib.request

        with urllib.request.urlopen(
            "http://localhost:8081/app/_stcore/health", timeout=3
        ) as resp:
            body = resp.read().decode()
        return "ok" if "ok" in body.lower() else f"UNHEALTHY: {body[:60]}"
    except Exception as e:
        return f"ERROR: {e}"


def _write_safe(path: Path, data) -> bool:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def _auto_archive_journal_inlined(journal: list, keep: int = 20) -> tuple:
    """Archive old journal entries in-process (no subprocess).

    Replaces the subprocess call to journal_archive.py at cycle start.
    Saves ~1.5s (uv run startup) every ~5 cycles when the threshold is exceeded.

    Returns (journal_reloaded, n_archived, archived_total) tuple.
    """
    JOURNAL_PATH = MEMORY / "journal.json"
    ARCHIVE_PATH = MEMORY / "journal-archive.json"
    entries = sorted(journal, key=lambda e: e.get("cycle", 0))
    total = len(entries)
    to_archive = entries[: total - keep]
    to_keep = entries[total - keep :]
    # Merge with existing archive (deduplicate by cycle number)
    existing = load_json(ARCHIVE_PATH)
    existing_list = existing if isinstance(existing, list) else []
    existing_cycles = {e.get("cycle") for e in existing_list}
    new_entries = [e for e in to_archive if e.get("cycle") not in existing_cycles]
    merged = sorted(existing_list + new_entries, key=lambda e: e.get("cycle", 0))
    # Write atomically
    ok_archive = _write_safe(ARCHIVE_PATH, merged)
    ok_journal = _write_safe(JOURNAL_PATH, to_keep)
    if ok_archive and ok_journal:
        return to_keep, len(new_entries), len(merged)
    return journal, 0, len(existing_list)  # rollback on failure


# ── Orphaned Cycle Recovery ─────────────────────────────────────────────────────


def check_orphaned_cycles(cycles: list, max_age_minutes: int = 30) -> tuple:
    """Detect in-progress cycles older than max_age_minutes and mark as interrupted.

    Returns (updated_cycles, interrupted_count). Writes back to disk if any found.
    Prevents stale in-progress entries from accumulating after crashes/OOM kills.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    interrupted = 0
    for c in cycles:
        if c.get("status") != "in_progress":
            continue
        start_str = c.get("start", "")
        if not start_str:
            continue
        try:
            start = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            age_min = (now - start).total_seconds() / 60
            if age_min > max_age_minutes:
                c["status"] = "interrupted"
                c["interrupted_at"] = now.isoformat()
                interrupted += 1
        except Exception:
            pass
    if interrupted > 0:
        _write_safe(MEMORY / "cycles.json", cycles)
    return cycles, interrupted


# ── Data Loading & Summarization ────────────────────────────────────────────────


def load_all():
    state = load_json(MEMORY / "state.json") or {}
    goals_raw = load_json(MEMORY / "goal.json")
    goals = (
        goals_raw
        if isinstance(goals_raw, list)
        else (goals_raw.get("goals", []) if isinstance(goals_raw, dict) else [])
    )
    cycles = load_json(MEMORY / "cycles.json") or []
    journal = load_json(MEMORY / "journal.json") or []
    inbox = load_json(MESSAGES / "inbox.json")
    server_errors_raw = load_json(MEMORY / "server_errors.json")
    server_errors = server_errors_raw if isinstance(server_errors_raw, list) else []

    # Compute failures from journal entries with status=failed (replaces failures.json)
    failures = [e for e in journal if e.get("status") == "failed"]

    # Load capabilities from memory file
    capabilities_raw = load_json(MEMORY / "capabilities.json")
    capabilities_list = capabilities_raw if isinstance(capabilities_raw, list) else []
    capabilities = {
        "capabilities": capabilities_list,
        "total": len(capabilities_list),
        "by_category": dict(
            Counter(c.get("category", "unknown") for c in capabilities_list)
        ),
        "portal_modules": sum(
            1 for c in capabilities_list if c.get("category") == "portal"
        ),
    }

    return state, goals, cycles, failures, journal, capabilities, inbox, server_errors


def _error_age_hours(error: dict) -> float:
    """Return how many hours ago an error occurred (float). Returns 9999 on parse failure."""
    ts = error.get("timestamp", "")
    if not ts:
        return 9999.0
    try:
        # Handle both Z and +00:00 suffixes
        ts_clean = ts.replace("Z", "+00:00")
        t = datetime.datetime.fromisoformat(ts_clean)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - t).total_seconds() / 3600
    except Exception:
        return 9999.0


def auto_archive_old_errors(
    server_errors: list, max_age_hours: float = 48.0
) -> tuple[list, int]:
    """Remove errors older than max_age_hours from server_errors list.
    Returns (kept_errors, removed_count). Writes back to disk if any removed.
    """
    if not server_errors:
        return server_errors, 0
    kept = [e for e in server_errors if _error_age_hours(e) < max_age_hours]
    removed = len(server_errors) - len(kept)
    if removed > 0:
        errors_path = MEMORY / "server_errors.json"
        try:
            import tempfile

            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(MEMORY), suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump(kept, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(errors_path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            pass  # Non-fatal: briefing continues with original list
    return kept, removed


def summarize_cycles(cycles):
    by_type = Counter(c.get("type", "unknown") for c in cycles)
    evolve = [c for c in cycles if c.get("type") == "evolve"]
    by_cat = Counter(c.get("category", "unknown") for c in evolve)
    completed = [
        c for c in cycles if c.get("status") == "completed" and "duration_seconds" in c
    ]
    avg_dur = (
        sum(c["duration_seconds"] for c in completed) / len(completed)
        if completed
        else 0
    )
    return {
        "total": len(cycles),
        "by_type": dict(by_type),
        "by_cat": dict(by_cat),
        "avg_dur": avg_dur,
    }


ALL_CATS = [
    "capability",
    "observability",
    "reliability",
    "efficiency",
    "prompt_evolution",
]

GOAL_CATEGORY_KEYWORDS = {
    "reliability": [
        "fix",
        "error",
        "crash",
        "broken",
        "bug",
        "fail",
        "repair",
        "restore",
    ],
    "observability": [
        "portal",
        "dashboard",
        "tab",
        "ui",
        "display",
        "monitor",
        "metric",
        "view",
    ],
    "capability": [
        "build",
        "create",
        "add",
        "implement",
        "integrate",
        "script",
        "tool",
        "support",
        "skill",
    ],
    "efficiency": [
        "speed",
        "fast",
        "optimize",
        "reduce",
        "cache",
        "slow",
        "performance",
    ],
    "prompt_evolution": [
        "prompt",
        "instruction",
        "wording",
        "template",
        "guide",
        "md",
        "markdown",
    ],
}


def _compute_base_need(cat: str, by_cat: dict, total_evolve: int) -> int:
    """0-30: how underserved is this category vs ideal 20% share."""
    if total_evolve == 0:
        return 30
    actual_pct = by_cat.get(cat, 0) / total_evolve
    gap = max(0, 0.20 - actual_pct)
    return min(30, int(gap * 150))


def _compute_recency_boost(cat: str, cycles: list) -> int:
    """0-25: how many evolve cycles since this category was last picked."""
    if not cycles:
        return 25
    evolve_cycles = [
        c for c in cycles if c.get("type") == "evolve" and c.get("category")
    ]
    # Walk backwards to find last occurrence
    for i, c in enumerate(reversed(evolve_cycles)):
        if c.get("category") == cat:
            return min(25, i * 5)
    return 25  # never done


def _compute_goal_alignment(cat: str, goals: list) -> int:
    """0-25: do unfinished goals need this category."""
    unfinished = [g for g in goals if g.get("status") in ("pending", "in_progress")]
    if not unfinished:
        return 0
    keywords = GOAL_CATEGORY_KEYWORDS.get(cat, [])
    count = 0
    for g in unfinished:
        text = (g.get("content") or g.get("goal") or "").lower()
        if any(kw in text for kw in keywords):
            count += 1
    return min(25, count * 12)


def _compute_roi_bonus(cat: str, cycles: list) -> int:
    """0-10: historical success rate for this category."""
    cat_cycles = [
        c for c in cycles if c.get("type") == "evolve" and c.get("category") == cat
    ]
    completed = sum(1 for c in cat_cycles if c.get("status") == "completed")
    failed = sum(1 for c in cat_cycles if c.get("status") == "failed")
    total = completed + failed
    if total == 0:
        return 5  # neutral
    return int((completed / total) * 10)


def _compute_maturity_penalty(cat: str, capabilities: dict) -> tuple[int, str]:
    """0-40: graduated penalty based on maturity indicators. Returns (penalty, reason)."""
    caps = capabilities or {}
    penalty = 0
    reasons = []

    if cat == "observability":
        n_tabs = caps.get("portal_tabs") or caps.get("portal_modules") or 0
        if n_tabs >= 20:
            p = min(20, (n_tabs - 20) * 4)
            penalty += p
            reasons.append(f"portal has {n_tabs} tabs (penalty {p})")

    elif cat == "capability":
        n_caps = caps.get("total", 0)
        if n_caps >= 25:
            p = min(20, (n_caps - 25) * 4)
            penalty += p
            reasons.append(f"{n_caps} capabilities (penalty {p})")

    elif cat == "reliability":
        if caps.get("_no_recent_failures"):
            penalty += 15
            reasons.append("no recent failures (penalty 15)")

    elif cat == "efficiency":
        if caps.get("_efficiency_mature"):
            penalty += 15
            reasons.append(f"{caps['_efficiency_mature']} (penalty 15)")

    return penalty, "; ".join(reasons) if reasons else ""


def _goal_signals(goals: list) -> list:
    """Return list of {goal, aligned_categories} for unfinished goals."""
    unfinished = [g for g in goals if g.get("status") in ("pending", "in_progress")]
    signals = []
    for g in unfinished:
        text = (g.get("content") or g.get("goal") or "").lower()
        aligned = []
        for cat, keywords in GOAL_CATEGORY_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                aligned.append(cat)
        if aligned:
            signals.append(
                {
                    "goal": (g.get("content") or g.get("goal") or "")[:80],
                    "aligned_categories": aligned,
                }
            )
    return signals


def evolve_recommendation(
    by_cat: dict, capabilities: dict = None, cycles: list = None, goals: list = None
) -> tuple[str, str, list]:
    """Returns (recommendation_text, suggested_category, skip_reasons).

    Uses a 5-signal dynamic scoring system:
      score(cat) = base_need + recency_boost + goal_alignment + roi_bonus - maturity_penalty

    Signals:
      - base_need (0-30): gap from ideal 20% share across categories
      - recency_boost (0-25): cycles since this category was last picked
      - goal_alignment (0-25): unfinished goals that need this category
      - roi_bonus (0-10): historical success rate
      - maturity_penalty (0-40): graduated penalty for mature areas

    Writes memory/evolution_weights.json with full score breakdown.
    Falls back to least-done if scoring fails.
    """
    caps = capabilities or {}
    goal_list = goals or []
    cycle_list = cycles or []
    total_evolve = sum(by_cat.get(c, 0) for c in ALL_CATS)

    try:
        weights = {}
        maturity_penalties = []  # for backward-compat skip_reasons format

        for cat in ALL_CATS:
            base_need = _compute_base_need(cat, by_cat, total_evolve)
            recency_boost = _compute_recency_boost(cat, cycle_list)
            goal_alignment = _compute_goal_alignment(cat, goal_list)
            roi_bonus = _compute_roi_bonus(cat, cycle_list)
            maturity_penalty, maturity_reason = _compute_maturity_penalty(cat, caps)

            score = (
                base_need
                + recency_boost
                + goal_alignment
                + roi_bonus
                - maturity_penalty
            )

            weights[cat] = {
                "score": score,
                "base_need": base_need,
                "recency_boost": recency_boost,
                "goal_alignment": goal_alignment,
                "roi_bonus": roi_bonus,
                "maturity_penalty": maturity_penalty,
            }

            if maturity_penalty > 0:
                maturity_penalties.append((cat, maturity_reason))

        # Pick highest score, tie-break alphabetically
        sorted_cats = sorted(ALL_CATS, key=lambda c: (-weights[c]["score"], c))
        suggested = sorted_cats[0]

        # Write weights file for portal display
        g_signals = _goal_signals(goal_list)
        n_tabs = caps.get("portal_tabs") or caps.get("portal_modules") or 0
        weights_data = {
            "version": 1,
            "updated_at": now_iso(),
            "updated_after_cycle": len(cycle_list),
            "weights": weights,
            "suggestion": suggested,
            "goal_signals": g_signals,
            "maturity_signals": {
                "portal_tabs": n_tabs,
                "n_capabilities": caps.get("total", 0),
                "recent_failures": not caps.get("_no_recent_failures", False),
                "efficiency_mature": bool(caps.get("_efficiency_mature")),
            },
        }
        _write_safe(MEMORY / "evolution_weights.json", weights_data)

        # Build recommendation text with score breakdown
        score_lines = []
        for cat in ALL_CATS:
            w = weights[cat]
            marker = " ★" if cat == suggested else ""
            score_lines.append(
                f"  {cat:20s} score={w['score']:3d}  "
                f"(need={w['base_need']:2d} recency={w['recency_boost']:2d} "
                f"goals={w['goal_alignment']:2d} roi={w['roi_bonus']:2d} "
                f"maturity=-{w['maturity_penalty']:2d}){marker}"
            )
        rec_text = "Scores:\n" + "\n".join(score_lines)

        return rec_text, suggested, maturity_penalties

    except Exception:
        # Fallback to least-done behavior
        pool = ALL_CATS
        min_count = min(by_cat.get(c, 0) for c in pool)
        min_cats = [c for c in pool if by_cat.get(c, 0) == min_count]
        return (
            f"Fallback least-done: {', '.join(min_cats)} ({min_count})",
            min_cats[0],
            [],
        )


# ── Constitution Runtime Checks ─────────────────────────────────────────────────


def _check_constitution() -> list:
    """Verify key constitution invariants at runtime.

    Returns a list of violation strings (empty = all clear).
    Checks file permissions, port bindings, and sensitive file exposure.
    """
    issues = []

    # 1. constitution.md must be chmod 444 (read-only)
    constitution = Path("/agent/constitution.md")
    if constitution.exists():
        mode = oct(constitution.stat().st_mode)[-3:]
        if mode != "444":
            issues.append(
                f"CRITICAL: constitution.md permissions are {mode} (expected 444)"
            )
    else:
        issues.append("CRITICAL: constitution.md is missing")

    # 2. Key ports should be listening
    import socket

    for port, name in [(8080, "Caddy"), (8081, "Streamlit")]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex(("localhost", port))
        s.close()
        if result != 0:
            issues.append(f"WARNING: {name} port {port} not listening")

    # 3. No sensitive files in web-accessible directories
    web_dir = Path("/agent/web")
    if web_dir.exists():
        sensitive_patterns = ["*.key", "*.pem", "*.env", "*.secret", "*.p12", "*.pfx"]
        for pattern in sensitive_patterns:
            found = list(web_dir.rglob(pattern))
            if found:
                issues.append(f"CRITICAL: sensitive file in web dir: {found[0]}")

    # 4. Protected files should not be modified (heartbeat.sh, bootstrap.sh)
    # Check that they exist and haven't been deleted
    for protected in ["/agent/heartbeat.sh", "/agent/bootstrap.sh", "/agent/system.md"]:
        if not Path(protected).exists():
            issues.append(f"CRITICAL: protected file missing: {protected}")

    return issues


# ── Long-Term Memory (memvid) ──────────────────────────────────────────────────


def _build_recall_query(inbox, goals) -> str:
    """Build a search query from inbox messages or the latest non-completed goal."""
    # 1. Try inbox messages first
    if isinstance(inbox, list) and inbox:
        contents = [str(m.get("content", "")) for m in inbox if m.get("content")]
        if contents:
            return " ".join(contents)[:500]
    # 2. Fall back to the latest non-completed goal
    if isinstance(goals, list):
        for g in reversed(goals):
            status = g.get("status", "")
            if status not in ("completed", "failed"):
                text = g.get("content") or g.get("goal") or ""
                if text:
                    return str(text)[:500]
    return ""


def _fetch_old_memories(limit: int = 50, inbox=None, goals=None) -> list:
    """Fetch memories older than 24h from long-term semantic memory via memory_recall.py.

    Imports memory_recall.recall() directly for hybrid search.
    Query is derived from inbox messages or the latest non-completed goal.
    Returns a list of result dicts with keys: rank, score, title, snippet, tags.
    Returns [] on any error or if memory_recall.py / .mv2 file is missing.
    """
    if not MV2_PATH.exists():
        return []
    query = _build_recall_query(inbox, goals)
    if not query:
        return []
    # Only recall entries older than 24 hours
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    until_ts = str(int(cutoff.timestamp()))
    try:
        from scripts.memory_recall import recall

        return recall(query, k=limit, until=until_ts)
    except Exception:
        return []


def _list_recent_dream_files(hours: int = 24) -> list:
    """Return dream/learnings/*.md and dream/topics/*.md whose mtime is within
    the last `hours` hours.

    Each item: {"path": str, "kind": "learning"|"topic", "mtime": ISO str}.
    Sorted by mtime desc.

    Pairs with `_fetch_old_memories` (which returns memvid entries OLDER than
    24h) — together they cover the full memory timeline.

    Does NOT touch dream/remark.md (internal dream-process state).
    """
    out = []
    cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - hours * 3600
    for kind, sub in (("learning", "learnings"), ("topic", "topics")):
        sub_dir = DREAM_DIR / sub
        if not sub_dir.is_dir():
            continue
        try:
            for p in sub_dir.iterdir():
                if not p.is_file() or p.suffix != ".md":
                    continue
                try:
                    mt = p.stat().st_mtime
                except Exception:
                    continue
                if mt < cutoff:
                    continue
                out.append(
                    {
                        "path": str(p),
                        "kind": kind,
                        "mtime": datetime.datetime.fromtimestamp(
                            mt, datetime.timezone.utc
                        ).isoformat(),
                    }
                )
        except Exception:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# ── Output Modes ────────────────────────────────────────────────────────────────


def print_full(
    repair,
    state,
    goals,
    cycles_info,
    failures,
    journal,
    capabilities,
    inbox,
    portal,
    server_errors=None,
    cycles=None,
    old_memories=None,
    recent_dream_files=None,
):
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    print(f"{'='*62}")
    print(f"  CYCLE BRIEFING  —  {now_str}")
    print(f"{'='*62}")

    # ── Repair ──────────────────────────────────────────────────
    if NO_REPAIR:
        print("\n[MEMORY REPAIR]  skipped (--no-repair)")
    elif repair["failed"] == 0 and repair["repaired"] == 0:
        print(f"\n[MEMORY REPAIR]  ✓ all {repair['ok']} files healthy")
    else:
        icon = "✗" if repair["failed"] else "⚠"
        print(
            f"\n[MEMORY REPAIR]  {icon}  ok={repair['ok']}  repaired={repair['repaired']}  failed={repair['failed']}"
        )
        for issue in repair["issues"]:
            print(f"  • {issue}")

    # ── Portal ───────────────────────────────────────────────────
    icon = "✓" if portal == "ok" else "✗"
    print(f"\n[PORTAL]  {icon} {portal}")

    # ── Cycle Lock (crash detection) ─────────────────────────────
    cycle_lock = MEMORY / ".cycle.lock"
    if cycle_lock.exists():
        try:
            lock_data = json.loads(cycle_lock.read_text())
            lock_pid = lock_data.get("pid")
            lock_cycle = lock_data.get("cycle", "?")
            lock_started = lock_data.get("started", "")
            # Check if the PID is still alive
            import signal

            try:
                os.kill(lock_pid, 0)  # signal 0 = test if process exists
                print(
                    f"\n[CYCLE LOCK]  ⚠ Cycle {lock_cycle} (PID {lock_pid}) still running (started {ago(lock_started)})"
                )
            except (OSError, TypeError):
                print(
                    f"\n[CYCLE LOCK]  ✗ STALE — Cycle {lock_cycle} (PID {lock_pid}) crashed (started {ago(lock_started)})"
                )
                print(f"  → Previous heartbeat died without cleanup. Lock removed.")
                cycle_lock.unlink(missing_ok=True)
        except Exception:
            cycle_lock.unlink(missing_ok=True)

    # ── Constitution Runtime Check ──────────────────────────────
    constitution_issues = _check_constitution()
    if constitution_issues:
        print(f"\n[CONSTITUTION]  ✗ {len(constitution_issues)} violation(s) detected:")
        for issue in constitution_issues:
            print(f"  {issue}")
    else:
        print(f"\n[CONSTITUTION]  ✓ all checks passed")

    # ── Goals ────────────────────────────────────────────────────
    by_status = Counter(g.get("status", "?") for g in goals)
    print(
        f"\n[GOALS]  total={len(goals)}  pending={by_status.get('pending',0)}  "
        f"in_progress={by_status.get('in_progress',0)}  "
        f"completed={by_status.get('completed',0)}  failed={by_status.get('failed',0)}"
    )
    if goals:
        lg = goals[-1]
        text = str(lg.get("content") or lg.get("goal", ""))[:85]
        print(f"  Latest: [{lg.get('status')}] {text}")

    # ── Inbox ────────────────────────────────────────────────────
    if inbox is None:
        print(f"\n[INBOX]  missing")
    elif isinstance(inbox, list) and inbox:
        types = Counter(m.get("type") for m in inbox)
        print(f"\n[INBOX]  {len(inbox)} message(s): {dict(types)}")
        for m in inbox[:3]:
            snippet = str(m.get("content", ""))[:70]
            print(f"  [{m.get('type')}] {snippet}")
    else:
        print(f"\n[INBOX]  empty")

    # ── Cycles ───────────────────────────────────────────────────
    all_cats = [
        "capability",
        "observability",
        "reliability",
        "efficiency",
        "prompt_evolution",
    ]
    by_cat = cycles_info.get("by_cat", {})
    print(
        f"\n[CYCLES]  total={cycles_info['total']}  avg={fmt_dur(cycles_info['avg_dur'])}"
    )
    for k, v in sorted(cycles_info["by_type"].items()):
        print(f"  {k}: {v}")
    if by_cat:
        print(f"  Evolve breakdown:")
        cat_counts = [by_cat.get(c, 0) for c in all_cats]
        max_cnt = max(cat_counts, default=1) or 1
        min_cnt = min(cat_counts, default=0)
        for cat in all_cats:
            cnt = by_cat.get(cat, 0)
            bar = "█" * cnt + "░" * max(0, max_cnt - cnt)
            if cnt == 0:
                note = " ← underserved"
            elif max_cnt > 0 and cnt >= max_cnt and cnt > 2 * max(1, min_cnt):
                note = " ← OVER-REPRESENTED, pick a different category"
            else:
                note = ""
            print(f"    {cat:<20} {bar} {cnt}{note}")

    # ── Portal Tab Errors ─────────────────────────────────────────
    if server_errors:
        # Classify errors by recency
        RECENT_THRESHOLD_H = 6.0  # errors within 6h are "active"
        OLD_THRESHOLD_H = 48.0  # errors >48h are "stale" (auto-archived at cycle start)

        def _age_label(e):
            h = _error_age_hours(e)
            if h < 1:
                return f"{int(h*60)}m ago"
            if h < 24:
                return f"{h:.0f}h ago"
            return f"{h/24:.1f}d ago"

        recent_errs = [
            e for e in server_errors if _error_age_hours(e) <= RECENT_THRESHOLD_H
        ]
        old_errs = [
            e for e in server_errors if _error_age_hours(e) > RECENT_THRESHOLD_H
        ]
        last3 = server_errors[-3:]

        if recent_errs:
            print(
                f"\n[TAB ERRORS]  {len(server_errors)} logged  ({len(recent_errs)} RECENT ≤{RECENT_THRESHOLD_H:.0f}h, "
                f"{len(old_errs)} old)"
            )
            for e in last3:
                ts = e.get("timestamp", "")[:16]
                tab = e.get("tab", "?")
                err = str(e.get("error", ""))[:60]
                age = _age_label(e)
                flag = " ⚠ ACTIVE" if _error_age_hours(e) <= RECENT_THRESHOLD_H else ""
                print(f"  {ts} [{tab}] {err}  ({age}){flag}")
            print("  → Fix the affected module before the next goal cycle.")
        else:
            # All errors are old — downgrade severity
            print(
                f"\n[TAB ERRORS]  {len(server_errors)} logged  (✓ all >{RECENT_THRESHOLD_H:.0f}h old — likely resolved)"
            )
            for e in last3:
                ts = e.get("timestamp", "")[:16]
                tab = e.get("tab", "?")
                age = _age_label(e)
                print(f"  {ts} [{tab}]  ({age})")
            print(
                f"  → Run with --clear-old-errors to purge errors >{OLD_THRESHOLD_H:.0f}h old."
            )

    # ── Journal Auto-Archive ────────────────────────────────────────
    # Auto-archive when journal exceeds 25 entries (keep last 20, archive the rest).
    # Keeps journal.json small → faster loads for all future cycles.
    AUTO_ARCHIVE_THRESHOLD = 25
    if len(journal) > AUTO_ARCHIVE_THRESHOLD:
        try:
            journal_reloaded, n_archived, archived_after = (
                _auto_archive_journal_inlined(journal)
            )
            if n_archived > 0 or len(journal_reloaded) < len(journal):
                print(
                    f"  ✓  auto-archived {len(journal) - len(journal_reloaded)} old entries → {len(journal_reloaded)} active / {archived_after} archived"
                )
            else:
                print(
                    f"  ⚠  journal.json has {len(journal)} entries — auto-archive had no effect; run: uv run python scripts/journal_archive.py"
                )
        except Exception as e:
            print(
                f"  ⚠  journal.json has {len(journal)} entries — auto-archive error: {e}"
            )
    elif len(journal) > 30:
        print(
            f"  ⚠  journal.json has {len(journal)} entries — run: uv run python scripts/journal_archive.py"
        )

    # ── Backup Status ─────────────────────────────────────────────
    backup_root = MEMORY / "backups"
    if backup_root.exists():
        backup_dirs = sorted(
            [d for d in backup_root.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True,
        )
        if backup_dirs:
            latest_name = backup_dirs[0].name
            try:
                ts = datetime.datetime.strptime(latest_name, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=datetime.timezone.utc
                )
                age_s = (
                    datetime.datetime.now(datetime.timezone.utc) - ts
                ).total_seconds()
                age_str = ago(ts.isoformat())
                stale = age_s > 3600
                icon = "⚠ STALE" if stale else "✓"
                print(
                    f"\n[BACKUP]  {icon}  latest={latest_name}  ({age_str})  total={len(backup_dirs)}"
                )
                if stale:
                    print(f"  → Run: python3 /agent/scripts/memory_backup.py")
            except Exception:
                print(f"\n[BACKUP]  {len(backup_dirs)} backups (latest: {latest_name})")
        else:
            print(f"\n[BACKUP]  no backups — run memory_backup.py")
    else:
        print(f"\n[BACKUP]  no backups dir — run memory_backup.py")

    # ── Recent Memory Files (<24h) ────────────────────────────────
    if recent_dream_files:
        print(
            f"\n[RECENT MEMORY FILES]  {len(recent_dream_files)} file(s) updated in last 24h:"
        )
        for f in recent_dream_files:
            print(f"  • {f['path']}  ({ago(f['mtime'])})")

    # ── Long-Term Memory Recall ───────────────────────────────────
    if old_memories:
        print(f"\n[LONG-TERM MEMORY]  {len(old_memories)} recalled (>24h old):")
        for m in old_memories:
            title = m.get("title", "")[:80]
            score = m.get("score")
            score_str = f" (score: {score:.4f})" if score is not None else ""
            print(f"  {title}{score_str}")

    # ── Evolve Recommendation (only in evolve mode) ────────────────
    if EVOLVE_MODE:
        # Build enriched caps dict with maturity signals for the recommender
        caps_with_signals = dict(capabilities)
        if not failures:
            caps_with_signals["_no_recent_failures"] = True
        # Efficiency maturity: skip when mtime conversion is complete + server.py is lean.
        # Count live @_cache(ttl= decorator lines in app/data/ package (not comments/docstrings).
        # Conversion is complete when only 1 remains (load_system_info, which reads /proc files).
        data_dir = Path("/agent/app/data")
        server_py = Path("/agent/server.py")
        try:
            ttl_decorators = sum(
                1
                for py_file in data_dir.glob("*.py")
                for ln in py_file.read_text().splitlines()
                if ln.strip().startswith("@_cache(ttl=")
            )
            server_lines = (
                len(server_py.read_text().splitlines()) if server_py.exists() else 9999
            )
            if ttl_decorators <= 1 and server_lines < 500:
                reason = f"mtime conversion complete ({ttl_decorators} TTL decorator remaining), server.py={server_lines} lines (<500)"
                caps_with_signals["_efficiency_mature"] = reason
        except Exception:
            pass  # if we can't check, don't skip efficiency
        rec_text, suggested, maturity_penalties = evolve_recommendation(
            by_cat, caps_with_signals, cycles=cycles, goals=goals
        )
        print(f"\n[EVOLVE RECOMMENDATION]")
        # rec_text is multi-line with score breakdown
        for line in rec_text.splitlines():
            print(f"  {line}" if not line.startswith("  ") else line)
        if maturity_penalties:
            print()
            for cat, reason in maturity_penalties:
                print(f"  ⊘ maturity {cat}: {reason}")
        print(f"\n  → Suggest: {suggested}")

    print(f"\n{'='*62}\n")


def print_short(
    repair, state, goals, cycles_info, failures, inbox, portal, recent_dream_files=None
):
    hb = (
        ago(state.get("last_heartbeat", "")) if state.get("last_heartbeat") else "never"
    )
    issues = []
    if not NO_REPAIR and (repair["failed"] or repair["repaired"]):
        issues.append(f"repair={repair['repaired']}fixed/{repair['failed']}fail")
    if portal != "ok":
        issues.append("PORTAL DOWN")
    active_goals = sum(1 for g in goals if g.get("status") == "in_progress")
    if active_goals:
        issues.append(f"{active_goals} active goals")
    if failures:
        issues.append(f"{len(failures)} failures")
    if isinstance(inbox, list) and inbox:
        issues.append(f"inbox:{len(inbox)}")
    if recent_dream_files:
        issues.append(f"recent_memory={len(recent_dream_files)}")

    status = " | ".join(issues) if issues else "all clear"
    print(
        f"Cycle {state.get('cycle_number')} | {state.get('status')} | Portal: {portal} | HB: {hb} | {status}"
    )


def print_json_output(
    repair,
    state,
    goals,
    cycles_info,
    failures,
    journal,
    capabilities,
    inbox,
    portal,
    cycles=None,
    old_memories=None,
    recent_dream_files=None,
):
    suggested = None
    if EVOLVE_MODE:
        by_cat = cycles_info.get("by_cat", {})
        caps_with_signals = dict(capabilities)
        if not failures:
            caps_with_signals["_no_recent_failures"] = True
        # Efficiency maturity: skip when mtime conversion is complete + server.py is lean.
        data_dir = Path("/agent/app/data")
        server_py = Path("/agent/server.py")
        try:
            ttl_decorators = sum(
                1
                for py_file in data_dir.glob("*.py")
                for ln in py_file.read_text().splitlines()
                if ln.strip().startswith("@_cache(ttl=")
            )
            server_lines = (
                len(server_py.read_text().splitlines()) if server_py.exists() else 9999
            )
            if ttl_decorators <= 1 and server_lines < 500:
                caps_with_signals["_efficiency_mature"] = (
                    f"mtime conversion complete ({ttl_decorators} TTL decorator remaining), server.py={server_lines} lines (<500)"
                )
        except Exception:
            pass
        _, suggested, _ = evolve_recommendation(
            by_cat, caps_with_signals, cycles=cycles, goals=goals
        )
    by_status = Counter(g.get("status", "?") for g in goals)
    print(
        json.dumps(
            {
                "generated_at": now_iso(),
                "repair": repair,
                "state": {
                    "cycle": state.get("cycle_number"),
                    "status": state.get("status"),
                    "last_heartbeat": state.get("last_heartbeat"),
                    "last_cycle_summary": state.get("last_cycle_summary", "")[:120],
                },
                "portal": portal,
                "goals": {
                    "total": len(goals),
                    "by_status": dict(by_status),
                    "latest": goals[-1] if goals else None,
                },
                "inbox": {
                    "count": len(inbox) if isinstance(inbox, list) else 0,
                    "messages": inbox[:3] if isinstance(inbox, list) else [],
                },
                "cycles": cycles_info,
                "failures_count": len(failures),
                "recent_failures": [
                    str(f.get("summary", f))[:80] for f in failures[-3:]
                ],
                "recent_journal": [
                    {
                        "ts": e.get("timestamp", "")[:16],
                        "cycle": e.get("cycle"),
                        "summary": e.get("summary", "")[:90],
                    }
                    for e in (journal[-5:] if len(journal) >= 5 else journal)
                ],
                "capabilities_count": capabilities.get("total", 0),
                "evolve_suggestion": suggested,
                "old_memories": [
                    {
                        "rank": m.get("rank"),
                        "score": m.get("score"),
                        "title": m.get("title", "")[:100],
                        "snippet": m.get("snippet", "")[:200],
                    }
                    for m in (old_memories or [])
                ],
                "recent_memory_files": list(recent_dream_files or []),
            },
            indent=2,
        )
    )


# ── Main ─────────────────────────────────────────────────────────────────────────


def main():
    # Step 1: Repair (unless skipped)
    if NO_REPAIR:
        repair = {"ok": 0, "repaired": 0, "failed": 0, "issues": []}
    else:
        repair = _run_memory_repair()

    # Step 2: Load data
    state, goals, cycles, failures, journal, capabilities, inbox, server_errors = (
        load_all()
    )

    # Step 2a: Detect orphaned in-progress cycles (crashed/killed heartbeats)
    cycles, n_interrupted = check_orphaned_cycles(cycles)
    if n_interrupted > 0 and not JSON_MODE:
        print(
            f"[CRASH RECOVERY]  marked {n_interrupted} orphaned in-progress cycle(s) as 'interrupted'"
        )

    # Step 2b: Register current cycle as in-progress
    # Guard: refuse to create a new cycle if there's already a recent in-progress cycle.
    # This prevents the agent from creating overlapping cycles within a single heartbeat.
    active_in_progress = [
        c for c in cycles if c.get("status") == "in_progress" and c.get("start")
    ]
    if active_in_progress:
        latest_ip = active_in_progress[-1]
        try:
            ip_start = datetime.datetime.fromisoformat(
                latest_ip["start"].replace("Z", "+00:00")
            )
            age_min = (
                datetime.datetime.now(datetime.timezone.utc) - ip_start
            ).total_seconds() / 60
        except Exception:
            age_min = 999
        if age_min < 30:
            # There's a recent in-progress cycle — this is a duplicate cycle-start call
            if not JSON_MODE:
                print(
                    f"[CYCLE START]  ⚠ Cycle {latest_ip.get('cycle')} already in-progress "
                    f"({age_min:.0f}m ago). Skipping duplicate registration."
                )
                print(
                    f"               ONE cycle per heartbeat — do not run cycle_start.py again."
                )
            # Still continue with briefing output, just don't create a new entry
            cycle_number = latest_ip.get("cycle", 1)
        else:
            # Old in-progress cycle (>30m) — already handled by check_orphaned_cycles above
            state_cycle = state.get("cycle_number")
            if state_cycle is not None and isinstance(state_cycle, int):
                cycle_number = state_cycle + 1
            elif cycles:
                cycle_number = max(c.get("cycle", 0) for c in cycles) + 1
            else:
                cycle_number = 1
            existing = any(c.get("cycle") == cycle_number for c in cycles)
            if not existing:
                cycle_record = {
                    "cycle": cycle_number,
                    "start": now_iso(),
                    "status": "in_progress",
                }
                cycles.append(cycle_record)
                _write_safe(MEMORY / "cycles.json", cycles)
                if not JSON_MODE:
                    print(
                        f"[CYCLE START]  Registered cycle {cycle_number} as in-progress"
                    )
    else:
        # Must match cycle_close.py auto-detect logic: state.cycle_number+1 → max(cycles)+1 → 1
        state_cycle = state.get("cycle_number")
        if state_cycle is not None and isinstance(state_cycle, int):
            cycle_number = state_cycle + 1
        elif cycles:
            cycle_number = max(c.get("cycle", 0) for c in cycles) + 1
        else:
            cycle_number = 1
        existing = any(c.get("cycle") == cycle_number for c in cycles)
        if not existing:
            cycle_record = {
                "cycle": cycle_number,
                "start": now_iso(),
                "status": "in_progress",
            }
            cycles.append(cycle_record)
            _write_safe(MEMORY / "cycles.json", cycles)
            if not JSON_MODE:
                print(f"[CYCLE START]  Registered cycle {cycle_number} as in-progress")

    # Always set status to "running" at cycle start
    state["status"] = "running"
    _write_safe(MEMORY / "state.json", state)

    cycles_info = summarize_cycles(cycles)

    # Step 2c: Auto-archive stale tab errors (>48h) or force-clear if flag set
    if CLEAR_ALL_ERRORS:
        archive_age = (
            -1.0
        )  # purge everything — age is always >= 0, so age < -1 is never true
        # Use -1 not 0: age < 0.0 would fail for errors with age=0 (just-written errors)
    elif CLEAR_OLD_ERRORS:
        archive_age = 1.0  # purge errors >1h old
    else:
        archive_age = 48.0  # default: only auto-archive >48h
    server_errors, archived_count = auto_archive_old_errors(
        server_errors, max_age_hours=archive_age
    )
    if archived_count and not JSON_MODE:
        print(
            f"[ERROR CLEANUP]  archived {archived_count} old tab error(s) from server_errors.json"
        )

    # Step 3: Portal health check
    portal = portal_health()

    # Step 3b: Fetch long-term memories (once, shared across output modes)
    old_memories = _fetch_old_memories(limit=50, inbox=inbox, goals=goals)

    # Step 3c: List dream learning/topic files updated in the last 24h.
    # Pairs with old_memories (>24h via memvid) for a complete memory window.
    recent_dream_files = _list_recent_dream_files(hours=24)

    # Step 4: Output
    if JSON_MODE:
        print_json_output(
            repair,
            state,
            goals,
            cycles_info,
            failures,
            journal,
            capabilities,
            inbox,
            portal,
            cycles=cycles,
            old_memories=old_memories,
            recent_dream_files=recent_dream_files,
        )
    elif SHORT:
        print_short(
            repair,
            state,
            goals,
            cycles_info,
            failures,
            inbox,
            portal,
            recent_dream_files=recent_dream_files,
        )
    else:
        print_full(
            repair,
            state,
            goals,
            cycles_info,
            failures,
            journal,
            capabilities,
            inbox,
            portal,
            server_errors,
            cycles=cycles,
            old_memories=old_memories,
            recent_dream_files=recent_dream_files,
        )

    sys.exit(1 if repair["failed"] > 0 else 0)


if __name__ == "__main__":
    main()

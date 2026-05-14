---
name: full-backup-and-migrate
description: Full container backup and cross-container migration for the agent. On the source agent, creates a single timestamped zip containing all critical agent directories (memory, messages, app, scripts, skills, services, prompts, web, workspace, and selective /home/agent/ files) and instructs the user how to download it. On the target agent, restores from the uploaded zip to migrate all knowledge, memory, capabilities, and workspace files to the new container/runtime.
---

# full-backup-and-migrate

**Backup script:** `scripts/full_backup.sh`
**Restore script:** `scripts/full_restore.sh`

Use this skill to migrate an agent from one container/runtime to another. The procedure has three roles:

1. **Source agent** — runs the backup script, then tells the user where to download the zip.
2. **User** — downloads the backup zip from the source container and uploads it to the target container.
3. **Target agent** — invokes this same skill to perform full recovery, restoring memory, messages, workspace, code, and home-dir credentials/config.

The result is that all agent knowledge, memory, capabilities, prompts, skills, services, and workspace files are migrated to the new container.

---

## When to use which mode

| Situation | Mode |
|-----------|------|
| You are the **source** agent and need to hand off to a new container | Run **Backup** (below), then give the user the **Handoff instructions**. |
| You are the **target** agent and a backup zip has been uploaded to `/agent/backup/` | Run **Recovery**. |

If unsure which side you are on, check whether `/agent/backup/agent_full_backup_*.zip` already exists and was *just uploaded* by the user (target side) versus produced by you (source side). When in doubt, ask the user.

---

## Backup (source agent)

### What gets backed up

| Directory | Notes |
|-----------|-------|
| `/agent/memory/` | All JSON memory files; excludes `long_term_memory.mv2` (auto-rebuilt by `cycle_close.py` on the next cycle close, in a detached background process, from the restored JSON files) |
| `/agent/messages/` | inbox/outbox queues and history |
| `/agent/web/` | Static files |
| `/agent/workspace/` | Working files and cached data |
| `/agent/app/` | Streamlit portal modules |
| `/agent/prompts/` | Prompt templates |
| `/agent/scripts/` | Automation scripts |
| `/agent/skills/` | Skill definitions |
| `/agent/services/` | Background service files |
| `/agent/` root files (selective) | `server.py`, `AGENTS.md`, `pyproject.toml`, `uv.lock`, `.streamlit/`, `test/` — packaged as `root_files.zip`. Forbidden-to-modify root files (constitution.md, system.md, agent.sh, heartbeat.sh, bootstrap.sh, Caddyfile, app/commands_tab.py, scripts/app_check.py) are omitted; they come back from bootstrap. |
| `/home/agent/` (selective) | `.keepass/`, `.ssh/`, `.config/`, `.claude/` (excluding the `-agent` workspace), and root dotfiles |
| `git.zip` (optional) | Git history bundle of `/agent/.git` — used in Phase 4 to apply only the delta for files that already exist in the target repo. Absent if `git bundle` failed. |
| `caddy_config.json` (optional) | Live Caddy admin-API config snapshot from `localhost:2019/config/`. Used in Phase 4.5 to restore agent-driven Caddy routes (the static `Caddyfile` is read-only). Absent if the admin API was unreachable when the backup ran. |

### What gets cleaned before zipping

Before creating any zip, the script deletes:

- `*.backup` files in `/agent/memory/`
- `*.tmp` files in `/agent/memory/`
- `.*.mv2.rebuild.*` hidden rebuild artifacts

The `/agent/memory/backups/` directory is moved to `/tmp/agent_memory_backups_<timestamp>/` (not deleted) so it can be manually restored if the backup fails.

### What gets excluded from zips

- `*.lock` files (except `uv.lock`, which is captured intentionally inside `root_files.zip`)
- `__pycache__/` directories
- `.pytest_cache/` directories (from `test/`)
- `.git/` directories — **except in `/agent/workspace/`, `/agent/web/`, and `/home/agent/`**, where nested `.git/` dirs of cloned repos are preserved. (Stripping them turns the clone into a plain dir tree; subsequent `git` commands then walk up to `/agent/.git` and silently attach to the wrong repo.)

### Output

```
/agent/backup/agent_full_backup_<YYYYMMDDTHHMMSSZ>.zip
  ├── memory.zip
  ├── messages.zip
  ├── web.zip
  ├── workspace.zip
  ├── app.zip
  ├── prompts.zip
  ├── scripts.zip
  ├── skills.zip
  ├── services.zip
  ├── home_agent.zip
  ├── root_files.zip      (loose, optional)
  └── caddy_config.json   (loose, optional)
```

### Usage

```bash
bash scripts/full_backup.sh
```

No arguments. The script prints progress and the final archive path and size.

### Handoff instructions (give these to the user verbatim)

After the backup completes, tell the user:

> Backup complete. Archive: `/agent/backup/agent_full_backup_<TIMESTAMP>.zip` (`<SIZE>`).
>
> **Next steps to migrate to the new agent container:**
>
> 1. **Download** the backup zip from this container to your local machine (portal file browser, or `scp <source-host>:/agent/backup/agent_full_backup_<TIMESTAMP>.zip ./`).
> 2. **Upload** the zip to the target agent container at `/agent/backup/` (create the directory if it does not exist):
>
>    ```bash
>    scp ./agent_full_backup_<TIMESTAMP>.zip <target-host>:/agent/backup/
>    ```
>
> 3. **On the target agent**, invoke this same skill (`full-backup-and-migrate`) and point it at the uploaded zip. The target agent will run the Recovery phases below.

Substitute the real timestamp, size, and host names. If the user's environment uses a different transport (cloud storage, browser download, etc.), adapt the wording but keep the three-step shape: download → upload → invoke skill on target.

### Optional aftercare (source side)

To restore the incremental memory snapshots that were moved aside:

```bash
mv /tmp/agent_memory_backups_<timestamp> /agent/memory/backups
```

---

## Recovery (target agent)

The deterministic phases of recovery are implemented in **`scripts/full_restore.sh`**. The agent runs the script; the script runs phases 0 → 5 in order. The agent only steps in for the few cases that genuinely need judgment (ambiguous backup zip, git merge conflicts, manual diff review when no `git.zip` is present, self-test failure).

### Step 1 — Locate the backup zip

The script does NOT auto-detect — you must pass the exact path. Find it under the standard upload locations first:

```bash
ls -1t /agent/backup/agent_full_backup_*.zip   2>/dev/null
ls -1t /agent/workspace/agent_full_backup_*.zip 2>/dev/null
```

Decide based on what you see:

- **Exactly one zip** across both directories → use it.
- **More than one zip** → **ask the user which one to restore** before continuing. Do not guess from filename timestamps; the user may have uploaded a fresh one alongside an older one and the right choice depends on intent.
- **None** → ask the user where they uploaded it, then locate it:

  ```bash
  find /agent /home/agent /tmp -maxdepth 5 -name "agent_full_backup_*.zip" 2>/dev/null
  ```

### Step 2 — Run the restore script

```bash
bash /agent/scripts/full_restore.sh /path/to/agent_full_backup_<TIMESTAMP>.zip
```

The path is required (no auto-detect — see Step 1).

The script runs these phases (mirrors the previous doc):

| Phase | What it does | Manual fallback? |
|-------|--------------|------------------|
| 0 | Stops services from `/agent/memory/services.json` | — |
| 1 | Unzips outer + inner zips into `/tmp/<restore_name>/` | — |
| 2 | Full-override copy of `memory/`, `messages/`, `workspace/`, `web/`, `home/agent/`, plus per-file restore of repo-root files (`server.py`, `AGENTS.md`, `pyproject.toml`, `uv.lock`, `.streamlit/`, `test/`) | — |
| 2b | `cd /agent && uv sync` if `pyproject.toml`/`uv.lock` changed | — |
| 3 | Selective copy of `app/`, `prompts/`, `scripts/`, `skills/`, `services/` (skips files that already exist on target); skipped paths logged to `/tmp/<restore_name>_skipped.txt` | — |
| 4a | Git-bundle delta apply for *tracked, modified* files only — **per-file, no commits**. For each file in Phase 3's skipped list that (a) differs from target and (b) is tracked in `/agent`'s git repo, the script generates a single-file patch from the bundle and tries `git apply --3way`. Files that apply cleanly are left as **uncommitted working-tree changes**. Files that fail or leave conflict markers are reverted to HEAD (`git checkout -- <file>`) and listed in `/tmp/<restore_name>_needs_merge.txt`. Untracked-but-divergent files go to `/tmp/<restore_name>_untracked.txt`. The script never aborts on conflict — it skips and moves on. | When the run finishes, **resolve files in `_needs_merge.txt` manually** (use the bundle, the backup tree, or `git diff` against the listed paths). For files in `_untracked.txt`, use the **Phase 4b** diff loop below. If `git.zip` is missing or no merge-base exists, the whole 4a path is skipped and **Phase 4b** is the only option. |
| 4.5 | POSTs `caddy_config.json` to `localhost:2019/load` to restore live Caddy routes (no-op if the file is absent) | If the POST fails: investigate Caddy with `ps`, container logs; re-run the snippet by hand. |
| 5 | Removes `/tmp/<restore_name>/`, moves backup zip into `/agent/backup/` if uploaded elsewhere, runs `uv run python scripts/self_test.py`, gateway probe, then `service_manager.py auto-start` + `health` | If `self_test.py` fails: read the output, fix the issue, then re-run the script with `--skip-self-test` once you've verified by hand. |

Script flags:

- `--skip-self-test` — skip the Phase 5 `self_test.py` + gateway probe.
- `--tail-only` — skip Phases 0–4a, run only Phase 4.5 + 5. Use after manually resolving Phase 4a conflicts so the already-restored `/agent/memory` and `/agent/workspace` are not overwritten a second time.
- `-h`, `--help` — usage.

**Resilience contract:** the script never aborts on a single-file failure. Bad inner zips, failed copies, conflict-on-apply, and unreachable Caddy admin all log a warning and continue. Only a self-test failure causes a non-zero exit, and even then services and Phase 4.5 still run. **On a self-test failure** the script leaves `/tmp/<restore_name>/` and the backup zip in place so you can re-run with `--tail-only` after fixing. On success it removes `/tmp/<restore_name>/` and moves the zip into `/agent/backup/`.

### Step 3 — Handle conflicts or 4b fallback if the script reports them

The script prints either:

- `Phase 4a conflicts (skipped, reverted to HEAD): N file(s) — see /tmp/<restore_name>_needs_merge.txt` → review each path in that log, reconcile against the backup tree (still at `/tmp/<restore_name>/agent/<rel>` if Phase 5 self-test failed; otherwise re-extract the backup zip), edit in `/agent/<rel>`, and leave the result as uncommitted working-tree changes. No need to re-run the script for this — Phases 0–4 already happened and Phase 4.5/5 succeeded. Only re-run `--tail-only` if the self-test was failing because of these unresolved files.
- `No git.zip in backup — fall back to manual diff review (Phase 4b).` → follow Phase 4b below.

#### Phase 4b — Manual diff review (fallback)

Use only when the script reported missing `git.zip` or no merge-base. The script left `/tmp/<restore_name>/` in place if it exited at Phase 4a, but cleared it if it reached Phase 5; in the cleared case, re-unzip the backup to a temp dir first.

```bash
RESTORE_DIR=/tmp/<restore_name>           # whatever the script printed
SKIPPED_LOG=/tmp/<restore_name>_skipped.txt

while IFS= read -r dst_file; do
  rel="${dst_file#/agent/}"
  src_file="$RESTORE_DIR/agent/$rel"

  [ -f "$src_file" ] || continue

  if diff -q "$src_file" "$dst_file" > /dev/null 2>&1; then
    echo "IDENTICAL (skip): $dst_file"
    continue
  fi

  echo ""
  echo "=== DIFF: $dst_file ==="
  diff "$src_file" "$dst_file" || true
done < "$SKIPPED_LOG"
```

Decide per file:

- **Identical** → already skipped automatically
- **Backup has new additions** → manually apply (e.g. new functions, new config keys)
- **Current is ahead** → keep current; backup is stale for this file
- **Both changed differently** → merge manually, keeping functional correctness of the current file

### Step 4 — Self-test failure troubleshooting

If the script exits with `Restore finished WITH WARNINGS (self_test rc=…)`:

1. Re-run `cd /agent && uv run python scripts/self_test.py` and read the failure.
2. Common causes: missing dep (run `uv sync`), missing env var, a service still down (`service_manager.py health`), a config file in `/agent/memory/` that didn't restore cleanly.
3. After fixing, re-run `bash /agent/scripts/full_restore.sh --tail-only [PATH_TO_ZIP]` to re-verify and bring services up. Add `--skip-self-test` if you've already verified by hand.

### Semantic memory index — auto-rebuilt by `cycle_close.py`

The backup intentionally omits `long_term_memory.mv2`. Do **not** run `memory_ingest.py --build` by hand — `cycle_close.py` detects the missing `.mv2` on the next cycle close and rebuilds it from the restored JSON files (`journal.json`, `journal_archive.json`, `messages/inbox_history.json`) in a detached background subprocess (see `scripts/cycle_close.py:_flush_memvid_buffer` and `_dispatch_memvid_flush_bg`). The cycle that triggers the rebuild does not need to wait for it; durability follows ~5–10s later, and progress is logged to `/agent/memory/.memvid_flush.log`.

```bash
uv run python scripts/memory_ingest.py --build
```

(Only run this manually if you need the index *immediately* and can't wait for the next cycle close.)

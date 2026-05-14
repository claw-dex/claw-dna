---
name: full-backup-and-migrate
description: Full container backup and cross-container migration for the agent. On the source agent, creates a single timestamped zip containing all critical agent directories (memory, messages, app, scripts, skills, services, prompts, web, workspace, and selective /home/agent/ files) and instructs the user how to download it. On the target agent, restores from the uploaded zip to migrate all knowledge, memory, capabilities, and workspace files to the new container/runtime.
---

# full-backup-and-migrate

**Path:** `scripts/full_backup.sh`

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
  ├── root_files.zip
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

Full recovery from an uploaded backup zip. The procedure has five phases. Before starting, locate the backup zip:

1. The user typically uploads to one of these two locations — check **both**:

   ```bash
   ls -lh /agent/backup/agent_full_backup_*.zip   2>/dev/null
   ls -lh /agent/workspace/agent_full_backup_*.zip 2>/dev/null
   ```

2. If multiple zips are found across both directories, ask the user which one to restore.
3. If **no** match is found in either location, ask the user to give the exact filename (or path) of the uploaded backup, then locate it with:

   ```bash
   find /agent /home/agent /tmp -maxdepth 5 -name "<filename>" 2>/dev/null
   ```

   (Substitute the name the user provides. Use `-iname` if the case is uncertain.)

Also confirm:

- The target directories (`/agent/`, `/home/agent/`) exist and you have write access.

### Phase 0 — Stop all running services

Before overwriting `/agent/memory/` (which contains `services.json` and the live PID/state of every background service), stop every service currently registered in `/agent/memory/services.json`. Leaving services running while the recovery copies over their state files causes stale PIDs, port conflicts, and partially-written memory files.

```bash
# Stop every service listed in services.json (skips ones already stopped)
for name in $(python3 -c "import json; print(' '.join(json.load(open('/agent/memory/services.json'))))" 2>/dev/null); do
  echo "Stopping $name ..."
  python3 /agent/scripts/service_manager.py stop "$name" || true
done

# Verify nothing is still running
python3 /agent/scripts/service_manager.py list
```

If `/agent/memory/services.json` does not yet exist on the target (fresh container), there is nothing to stop — skip this phase.

After recovery completes (end of Phase 5), services with `auto_start: true` will come back up via:

```bash
python3 /agent/scripts/service_manager.py auto-start
```

### Phase 1 — Unzip the backup

Set `BACKUP_ZIP` to the path you located above. Examples:

```bash
# Common case — uploaded to /agent/backup/
BACKUP_ZIP=/agent/backup/agent_full_backup_20260513T025937Z.zip

# Or, uploaded to the workspace dir
# BACKUP_ZIP=/agent/workspace/agent_full_backup_20260513T061915Z.zip
RESTORE_NAME=$(basename "$BACKUP_ZIP" .zip)
RESTORE_DIR=/tmp/${RESTORE_NAME}

mkdir -p "$RESTORE_DIR"

# Unzip the outer archive
unzip "$BACKUP_ZIP" -d "$RESTORE_DIR"

# Unzip each inner zip in the same directory
for z in "$RESTORE_DIR"/*.zip; do
  unzip "$z" -d "$RESTORE_DIR"
done
```

After this step, `$RESTORE_DIR` contains both the inner `.zip` files and the fully extracted directory tree (e.g. `agent/memory/`, `agent/scripts/`, `home/agent/`, etc.).

---

### Phase 2 — Full override copy (critical runtime data)

The following directories are fully overridden with backup content — existing files are replaced unconditionally:

| Backup path (inside `$RESTORE_DIR`) | Target on disk |
|--------------------------------------|----------------|
| `agent/memory/` | `/agent/memory/` |
| `agent/messages/` | `/agent/messages/` |
| `agent/workspace/` | `/agent/workspace/` |
| `agent/web/` | `/agent/web/` |
| `home/agent/` | `/home/agent/` |

```bash
cp -rf "$RESTORE_DIR/agent/memory/."    /agent/memory/
cp -rf "$RESTORE_DIR/agent/messages/."  /agent/messages/
cp -rf "$RESTORE_DIR/agent/workspace/." /agent/workspace/
cp -rf "$RESTORE_DIR/agent/web/."       /agent/web/
cp -rf "$RESTORE_DIR/home/agent/."      /home/agent/

# Repo-root agent-modifiable files (optional — `root_files.zip` is absent
# in backups produced before this artifact was introduced). Copied
# individually (NOT as `cp -rf agent/. /agent/`) so forbidden bootstrap
# files at /agent/ (constitution.md, system.md, agent.sh, etc.) are not
# touched. Each path is independently guarded so a partial backup still
# restores whatever it does contain.
if [ -f "$RESTORE_DIR/root_files.zip" ] || [ -e "$RESTORE_DIR/agent/server.py" ] \
   || [ -d "$RESTORE_DIR/agent/.streamlit" ] || [ -d "$RESTORE_DIR/agent/test" ]; then
  for p in server.py AGENTS.md pyproject.toml uv.lock; do
    [ -f "$RESTORE_DIR/agent/$p" ] && cp -f "$RESTORE_DIR/agent/$p" "/agent/$p"
  done
  [ -d "$RESTORE_DIR/agent/.streamlit" ] && cp -rf "$RESTORE_DIR/agent/.streamlit/." /agent/.streamlit/
  [ -d "$RESTORE_DIR/agent/test" ]       && cp -rf "$RESTORE_DIR/agent/test/."       /agent/test/
  echo "Restored repo-root agent-modifiable files."
else
  echo "No root_files.zip in this backup — skipping repo-root file restore (older backup format)."
fi
```

> **Note:** `cp -rf <src>/. <dst>/` copies directory *contents* (not the directory itself) into the target, overriding any matching files.

> **Dependency sync:** if `pyproject.toml` or `uv.lock` changed, run `cd /agent && uv sync` **before** Phase 5's `self_test.py` so the venv matches the restored manifest.

---

### Phase 3 — Selective copy (code directories, no override)

The remaining directories — `app`, `prompts`, `scripts`, `skills`, `services` — contain code that may have evolved on the target since the backup was taken. Copy only files that do **not** already exist in the target, and track every skipped file.

```bash
SKIPPED_LOG=/tmp/${RESTORE_NAME}_skipped.txt
> "$SKIPPED_LOG"   # reset log

selective_copy() {
  local src_root=$1
  local dst_root=$2

  find "$src_root" -type f | while read -r src_file; do
    rel="${src_file#$src_root/}"
    dst_file="$dst_root/$rel"

    if [ -e "$dst_file" ]; then
      echo "$dst_file" >> "$SKIPPED_LOG"
    else
      mkdir -p "$(dirname "$dst_file")"
      cp "$src_file" "$dst_file"
    fi
  done
}

selective_copy "$RESTORE_DIR/agent/app"      /agent/app
selective_copy "$RESTORE_DIR/agent/prompts"  /agent/prompts
selective_copy "$RESTORE_DIR/agent/scripts"  /agent/scripts
selective_copy "$RESTORE_DIR/agent/skills"   /agent/skills
selective_copy "$RESTORE_DIR/agent/services" /agent/services
```

After this step, review `$SKIPPED_LOG` to see which files were skipped.

---

### Phase 4 — Merge skipped files (git-bundle delta apply)

Skipped files are ones that already exist in the target repo. The goal is to apply only the changes the backup introduces *after* the common ancestor — not re-apply changes already present in the target's git history, and not overwrite local-only work.

#### 4a — Attempt git-bundle-based merge (preferred)

If `git.zip` is present in the backup, use the git history to compute and apply the exact delta:

```bash
BUNDLE_ZIP="$RESTORE_DIR/git.zip"
BACKUP_REPO=/tmp/${RESTORE_NAME}_git

if [ -f "$BUNDLE_ZIP" ]; then
  # Extract bundle and clone into a temp repo
  unzip -j "$BUNDLE_ZIP" git_history.bundle -d /tmp/
  git clone /tmp/git_history.bundle "$BACKUP_REPO"
  rm /tmp/git_history.bundle

  # Add backup repo as a remote and fetch its refs into the current repo
  git -C /agent remote add _backup "$BACKUP_REPO" 2>/dev/null || true
  git -C /agent fetch _backup --quiet

  BACKUP_HEAD=$(git -C "$BACKUP_REPO" rev-parse HEAD)
  MERGE_BASE=$(git -C /agent merge-base HEAD "$BACKUP_HEAD" 2>/dev/null || echo "")

  if [ -n "$MERGE_BASE" ]; then
    echo "Merge base: $MERGE_BASE"
    echo "Applying delta ($MERGE_BASE → $BACKUP_HEAD) for skipped files only..."

    # Build a pathspec from the skipped files so we only patch those
    PATHSPECS=()
    while IFS= read -r dst_file; do
      rel="${dst_file#/agent/}"
      src_file="$RESTORE_DIR/agent/$rel"
      [ -f "$src_file" ] || continue
      diff -q "$src_file" "$dst_file" > /dev/null 2>&1 && continue  # identical, skip
      PATHSPECS+=("$rel")
    done < "$SKIPPED_LOG"

    if [ ${#PATHSPECS[@]} -gt 0 ]; then
      git -C /agent diff "$MERGE_BASE" "$BACKUP_HEAD" -- "${PATHSPECS[@]}" \
        | git -C /agent apply --3way --ignore-whitespace 2>&1 || {
          echo "WARNING: some hunks failed to apply cleanly — check 'git status' for conflicts"
        }
    else
      echo "All skipped files are identical to backup — nothing to apply."
    fi
  else
    echo "No common ancestor found — falling back to manual diff review (4b)"
  fi

  # Cleanup
  git -C /agent remote remove _backup 2>/dev/null || true
  rm -rf "$BACKUP_REPO"
else
  echo "No git.zip in backup — falling back to manual diff review (4b)"
fi
```

After this step, run `git status` and `git diff` to review what was applied. Resolve any conflicts marked by `git apply --3way` before proceeding.

#### 4b — Fallback: manual diff review

Use this only when `git.zip` is absent (older backup) or the merge-base could not be found.

```bash
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

Review each diff and decide per file:

- **Identical** → already skipped automatically
- **Backup has new additions** → manually apply (e.g. new functions, new config keys)
- **Current is ahead** → keep current; backup is stale for this file
- **Both changed differently** → merge manually, keeping functional correctness of the current file

---

### Phase 4.5 — Restore Caddy live config

The static `/agent/Caddyfile` is read-only per the constitution; the agent reconfigures Caddy at runtime via the admin API on port 2019. Reapply the captured live config so routes/handlers the source agent added are present on the target. **Optional** — `caddy_config.json` is absent in backups produced before this artifact was introduced, and may also be absent if the source's admin API was unreachable at backup time; the whole phase no-ops in that case and Caddy keeps the bootstrap config.

Caddy is started by the process manager (PID 1) at container boot, so the admin API should already be listening by the time recovery reaches this phase. If the POST fails, fix Caddy (`ps`, container logs) and re-run the snippet.

```bash
CADDY_JSON="$RESTORE_DIR/caddy_config.json"
if [ -f "$CADDY_JSON" ]; then
  if curl -fsS -X POST http://localhost:2019/load \
       -H 'Content-Type: application/json' \
       --data-binary "@$CADDY_JSON"; then
    echo "Caddy live config restored from backup."
  else
    echo "WARNING: failed to POST caddy_config.json to :2019/load — restore manually."
  fi
else
  echo "No caddy_config.json in backup — Caddy keeps its current (bootstrap) config."
fi
```

Verify the reload took effect:

```bash
curl -fsS http://localhost:2019/config/ | diff - "$CADDY_JSON" >/dev/null \
  && echo "Caddy config matches backup." \
  || echo "Caddy config differs from backup — inspect manually."
```

---

### Phase 5 — Cleanup and verification

Remove the temp restore directory after confirming the system is healthy:

```bash
rm -rf "$RESTORE_DIR"
echo "Cleanup complete: $RESTORE_DIR removed"
```

If `$BACKUP_ZIP` was located outside `/agent/backup/` (e.g. uploaded to `/agent/workspace/`), move it into `/agent/backup/` now so future restores find it in the canonical location:

```bash
if [ "$(dirname "$BACKUP_ZIP")" != "/agent/backup" ]; then
  mkdir -p /agent/backup
  mv "$BACKUP_ZIP" /agent/backup/
  echo "Moved backup zip to: /agent/backup/$(basename "$BACKUP_ZIP")"
fi
```

Run the portal self-test to confirm nothing is broken (run `uv sync` first if `pyproject.toml`/`uv.lock` were restored in Phase 2):

```bash
uv run python scripts/self_test.py
curl -fsS http://localhost:8080/app/ -o /dev/null && echo "Gateway OK"
```

Start the background services that were stopped in Phase 0. This reads the **restored** `/agent/memory/services.json` and brings up every entry with `auto_start: true`:

```bash
python3 /agent/scripts/service_manager.py auto-start
python3 /agent/scripts/service_manager.py health
```

### Semantic memory index — auto-rebuilt by `cycle_close.py`

The backup intentionally omits `long_term_memory.mv2`. Do **not** run `memory_ingest.py --build` by hand — `cycle_close.py` detects the missing `.mv2` on the next cycle close and rebuilds it from the restored JSON files (`journal.json`, `journal_archive.json`, `messages/inbox_history.json`) in a detached background subprocess (see `scripts/cycle_close.py:_flush_memvid_buffer` and `_dispatch_memvid_flush_bg`). The cycle that triggers the rebuild does not need to wait for it; durability follows ~5–10s later, and progress is logged to `/agent/memory/.memvid_flush.log`.

```bash
uv run python scripts/memory_ingest.py --build
```

Otherwise, once the self-test passes and services are healthy, the migration is done — the target agent now holds the source agent's knowledge, memory, capabilities, and workspace. The `.mv2` will appear after the next cycle close.

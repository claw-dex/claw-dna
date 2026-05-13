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
| `/agent/memory/` | All JSON memory files; excludes `long_term_memory.mv2` (rebuilt via `memory_ingest`) |
| `/agent/messages/` | inbox/outbox queues and history |
| `/agent/web/` | Static files |
| `/agent/workspace/` | Working files and cached data |
| `/agent/app/` | Streamlit portal modules |
| `/agent/prompts/` | Prompt templates |
| `/agent/scripts/` | Automation scripts |
| `/agent/skills/` | Skill definitions |
| `/agent/services/` | Background service files |
| `/home/agent/` (selective) | `.keepass/`, `.ssh/`, `.config/`, `.claude/` (excluding the `-agent` workspace), and root dotfiles |

### What gets cleaned before zipping

Before creating any zip, the script deletes:

- `*.backup` files in `/agent/memory/`
- `*.tmp` files in `/agent/memory/`
- `.*.mv2.rebuild.*` hidden rebuild artifacts
- `long_term_memory.mv2` (874 MB — rebuild with `memory_ingest` after restore)

The `/agent/memory/backups/` directory is moved to `/tmp/agent_memory_backups_<timestamp>/` (not deleted) so it can be manually restored if the backup fails.

### What gets excluded from zips

- `*.lock` files
- `__pycache__/` directories
- `.git/` directories

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
  └── home_agent.zip
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
```

> **Note:** `cp -rf <src>/. <dst>/` copies directory *contents* (not the directory itself) into the target, overriding any matching files.

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

### Phase 4 — Merge skipped files (diff and selective apply)

For each skipped file, compare it against the backup version and selectively incorporate new changes from the backup without breaking the current file.

```bash
while IFS= read -r dst_file; do
  rel="${dst_file#/agent/}"
  # Find the corresponding backup source
  src_file="$RESTORE_DIR/agent/$rel"

  [ -f "$src_file" ] || continue

  # Skip if files are identical
  if diff -q "$src_file" "$dst_file" > /dev/null 2>&1; then
    echo "IDENTICAL (skip): $dst_file"
    continue
  fi

  echo ""
  echo "=== DIFF: $dst_file ==="
  diff "$src_file" "$dst_file" || true
done < "$SKIPPED_LOG"
```

Review each diff output and decide per file:

- **Identical** → already skipped automatically
- **Backup has new additions** → manually apply them (e.g. new functions, new config keys added after the backup was taken)
- **Current is ahead** → keep current; backup is stale for this file
- **Both changed differently** → merge manually, keeping functional correctness of the current file

> For code files (`.py`, `.sh`, `.md`), prefer reading the diff and applying only clearly new/additive changes. Never blindly overwrite a current file that has been actively modified since the backup.

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

Run the portal self-test to confirm nothing is broken:

```bash
uv run python scripts/self_test.py
```

Rebuild the semantic memory index (the backup intentionally omits `long_term_memory.mv2`):

```bash
uv run python scripts/memory_ingest.py --build
```

Once the self-test passes and memory ingest completes, the migration is done — the target agent now holds the source agent's knowledge, memory, capabilities, and workspace.

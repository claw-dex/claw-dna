#!/usr/bin/env bash
# Full agent backup — creates a single timestamped zip of all critical directories.
# long_term_memory.mv2 is excluded (rebuild via: uv run python scripts/memory_ingest.py)
set -euo pipefail

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
STAGING=/tmp/agent_backup_parts_${TIMESTAMP}
BACKUP_DIR=/agent/backup
FINAL_ZIP=${BACKUP_DIR}/agent_full_backup_${TIMESTAMP}.zip

mkdir -p "$STAGING" "$BACKUP_DIR"

# ── Phase 1: Cleanup ──────────────────────────────────────────────────────────
echo "[1/4] Cleaning temp/rebuild/backup files from /agent/memory/ ..."

find /agent/memory -maxdepth 1 -name "*.backup" -delete -print 2>/dev/null || true
find /agent/memory -maxdepth 1 -name "*.tmp" -delete -print 2>/dev/null || true
find /agent/memory -maxdepth 1 -name ".*.mv2.rebuild.*" -delete -print 2>/dev/null || true

# Exclude long_term_memory.mv2 — rebuilt via memory_ingest skill
if [ -f /agent/memory/long_term_memory.mv2 ]; then
  echo "  Removing long_term_memory.mv2 (rebuild via memory_ingest)"
  rm -f /agent/memory/long_term_memory.mv2
else
  echo "  long_term_memory.mv2 already absent, skipping."
fi

# ── Phase 2: Individual zip archives ─────────────────────────────────────────
echo ""
echo "[2/4] Creating individual zip archives in $STAGING ..."

zip_dir() {
  local name=$1
  local src=$2
  echo "  → ${name}.zip  ($src)"
  zip -r "$STAGING/${name}.zip" "$src" \
    --exclude "*.lock" \
    --exclude "*/__pycache__/*" \
    --exclude "*/.pytest_cache/*" \
    --exclude "*/.git/*" \
    --exclude "/agent/scripts/full_backup.sh" \
    --exclude "/agent/scripts/full_restore.sh" \
    --exclude "/agent/skills/full-backup-and-migrate/*"
}

# /agent/workspace/, /agent/web/, and /home/agent/ can contain cloned repos
# (e.g. /agent/workspace/<project>/.git/). Do NOT exclude .git for these —
# stripping it leaves a directory tree with no repo metadata, and any later
# `git` command run inside walks up the parent chain and silently attaches
# to /agent/.git instead. Keep .git dirs intact so clones restore as real
# git working trees.
zip_dir_keepgit() {
  local name=$1
  local src=$2
  echo "  → ${name}.zip  ($src, .git preserved)"
  zip -r "$STAGING/${name}.zip" "$src" \
    --exclude "*.lock" \
    --exclude "*/__pycache__/*"
}

zip_dir          memory    /agent/memory
zip_dir          messages  /agent/messages
zip_dir_keepgit  web       /agent/web
zip_dir_keepgit  workspace /agent/workspace
zip_dir          app       /agent/app
zip_dir          prompts   /agent/prompts
zip_dir          scripts   /agent/scripts
zip_dir          skills    /agent/skills
zip_dir          services  /agent/services
# Tests get their own archive. Restore uses "prefer-current" semantics for
# this tree (existing test files on the target are kept), so the test.zip
# only seeds new test files on a fresh container.
zip_dir          test      /agent/test

# Repo-root agent-modifiable files (constitution permits: /agent/*.py,
# AGENTS.md, pyproject.toml, .streamlit/config.toml). Forbidden-to-modify
# items (constitution.md, system.md, agent.sh, heartbeat.sh, bootstrap.sh,
# Caddyfile, app/commands_tab.py, scripts/app_check.py) are intentionally
# omitted — they come back from bootstrap on the target.
echo "  → root_files.zip  (repo-root agent-modifiable files)"
ROOT_PARTS=()
for p in \
  /agent/server.py \
  /agent/AGENTS.md \
  /agent/pyproject.toml \
  /agent/uv.lock \
  /agent/.streamlit
do
  [ -e "$p" ] && ROOT_PARTS+=("$p")
done
# No `--exclude "*.lock"` here — uv.lock is intentionally included.
if [ ${#ROOT_PARTS[@]} -gt 0 ]; then
  zip -r "$STAGING/root_files.zip" \
    "${ROOT_PARTS[@]}" \
    --exclude "*/__pycache__/*" \
    --exclude "*/.git/*"
else
  echo "  WARNING: none of the expected root files exist — skipping root_files.zip"
fi

# Caddy live admin-API config. The static Caddyfile is read-only per
# constitution; agent-driven changes go through :2019, so capture that
# state here. Loose JSON (not zipped) so restore can `curl --data-binary @`.
echo "  → caddy_config.json  (live Caddy admin-API config dump)"
if curl -fsS http://localhost:2019/config/ -o "$STAGING/caddy_config.json"; then
  echo "  Caddy config captured ($(wc -c <"$STAGING/caddy_config.json") bytes)"
else
  echo "  WARNING: Caddy admin API not reachable at :2019 — skipping caddy_config.json"
  rm -f "$STAGING/caddy_config.json"
fi

# Git history bundle — used during restore to resolve modified files only.
# New files are copied directly; this bundle lets the target find the merge-base
# and apply only the delta for files that already exist in the target repo.
echo "  → git.zip  (git history bundle from /agent/.git)"
if git -C /agent bundle create "$STAGING/git_history.bundle" --all 2>/dev/null; then
  (cd "$STAGING" && zip git.zip git_history.bundle && rm git_history.bundle)
  echo "  Git bundle created successfully."
else
  echo "  WARNING: git bundle failed — restore will fall back to manual diff review for modified files"
fi

# Git HEAD SHA — lightweight reference for restore ancestry checks.
# Lets the restore determine whether the target's git is ahead or behind
# the backup even when the full bundle is absent.
echo "  → git_head.txt  (backup git HEAD SHA)"
if git -C /agent rev-parse HEAD > "$STAGING/git_head.txt" 2>/dev/null; then
  echo "  Git HEAD: $(cat "$STAGING/git_head.txt")"
else
  echo "  WARNING: could not capture git HEAD SHA — restore ancestry checks will be skipped"
  rm -f "$STAGING/git_head.txt"
fi

# home_agent — selective: credentials + configs + .claude (minus heavy workspace)
echo "  → home_agent.zip  (selective paths from /home/agent/)"
HOME_PARTS=()
for p in \
  /home/agent/.keepass \
  /home/agent/.ssh \
  /home/agent/.config \
  /home/agent/.claude \
  /home/agent/.bashrc \
  /home/agent/.gitconfig \
  /home/agent/.profile
do
  [ -e "$p" ] && HOME_PARTS+=("$p")
done

# Exclude credential/session/runtime state from the source container so a
# restore on a new host doesn't overwrite fresh tokens with expired ones.
zip -r "$STAGING/home_agent.zip" \
  "${HOME_PARTS[@]}" \
  --exclude "*.lock" \
  --exclude "*/__pycache__/*" \
  --exclude "/home/agent/.claude/projects/-agent/*" \
  --exclude "/home/agent/.claude/.credentials.json" \
  --exclude "/home/agent/.claude/mcp-needs-auth-cache.json" \
  --exclude "/home/agent/.claude/daemon/*" \
  --exclude "/home/agent/.claude/daemon.lock" \
  --exclude "/home/agent/.claude/daemon.log" \
  --exclude "/home/agent/.claude/daemon.status.json" \
  --exclude "/home/agent/.claude/ide/*" \
  --exclude "/home/agent/.claude/sessions/*"

# ── Phase 3: Final archive ────────────────────────────────────────────────────
echo ""
echo "[3/4] Moving individual zips to $BACKUP_DIR ..."
mv "$STAGING"/*.zip "$BACKUP_DIR/"
mv "$STAGING/caddy_config.json" "$BACKUP_DIR/" 2>/dev/null || true
mv "$STAGING/git_head.txt" "$BACKUP_DIR/" 2>/dev/null || true
rmdir "$STAGING"

echo "[3/4] Creating final archive: $FINAL_ZIP"
cd "$BACKUP_DIR"
ARCHIVE_PARTS=(memory.zip messages.zip web.zip workspace.zip app.zip prompts.zip scripts.zip skills.zip services.zip home_agent.zip)
[ -f test.zip ] && ARCHIVE_PARTS+=(test.zip)
[ -f root_files.zip ] && ARCHIVE_PARTS+=(root_files.zip)
[ -f git.zip ] && ARCHIVE_PARTS+=(git.zip)
[ -f caddy_config.json ] && ARCHIVE_PARTS+=(caddy_config.json)
[ -f git_head.txt ] && ARCHIVE_PARTS+=(git_head.txt)
zip "$FINAL_ZIP" "${ARCHIVE_PARTS[@]}"

echo "[3/4] Removing individual zip files ..."
rm -f \
  "$BACKUP_DIR/memory.zip" \
  "$BACKUP_DIR/messages.zip" \
  "$BACKUP_DIR/web.zip" \
  "$BACKUP_DIR/workspace.zip" \
  "$BACKUP_DIR/app.zip" \
  "$BACKUP_DIR/prompts.zip" \
  "$BACKUP_DIR/scripts.zip" \
  "$BACKUP_DIR/skills.zip" \
  "$BACKUP_DIR/services.zip" \
  "$BACKUP_DIR/home_agent.zip" \
  "$BACKUP_DIR/test.zip" \
  "$BACKUP_DIR/root_files.zip" \
  "$BACKUP_DIR/caddy_config.json" \
  "$BACKUP_DIR/git.zip" \
  "$BACKUP_DIR/git_head.txt"

# ── Phase 4: Done ─────────────────────────────────────────────────────────────
SIZE=$(du -sh "$FINAL_ZIP" | cut -f1)
echo ""
echo "[4/4] Backup complete."
echo "  Archive : $FINAL_ZIP"
echo "  Size    : $SIZE"
echo ""
echo "  To rebuild long_term_memory.mv2 after restore:"
echo "    cd /agent && uv run python scripts/memory_ingest.py"

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

echo "[1/4] Moving /agent/memory/backups to /tmp ..."
if [ -d /agent/memory/backups ]; then
  mv /agent/memory/backups /tmp/agent_memory_backups_${TIMESTAMP}
  echo "  Moved to: /tmp/agent_memory_backups_${TIMESTAMP}"
else
  echo "  /agent/memory/backups not found, skipping."
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
    --exclude "*/.git/*"
}

zip_dir memory    /agent/memory
zip_dir messages  /agent/messages
zip_dir web       /agent/web
zip_dir workspace /agent/workspace
zip_dir app       /agent/app
zip_dir prompts   /agent/prompts
zip_dir scripts   /agent/scripts
zip_dir skills    /agent/skills
zip_dir services  /agent/services

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
  /home/agent/.profile \
  /home/agent/.claude.json
do
  [ -e "$p" ] && HOME_PARTS+=("$p")
done

zip -r "$STAGING/home_agent.zip" \
  "${HOME_PARTS[@]}" \
  --exclude "*.lock" \
  --exclude "*/__pycache__/*" \
  --exclude "*/.git/*" \
  --exclude "/home/agent/.claude/projects/-agent/*"

# ── Phase 3: Final archive ────────────────────────────────────────────────────
echo ""
echo "[3/4] Moving individual zips to $BACKUP_DIR ..."
mv "$STAGING"/*.zip "$BACKUP_DIR/"
rmdir "$STAGING"

echo "[3/4] Creating final archive: $FINAL_ZIP"
cd "$BACKUP_DIR"
zip "$FINAL_ZIP" \
  memory.zip messages.zip web.zip workspace.zip app.zip \
  prompts.zip scripts.zip skills.zip services.zip home_agent.zip

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
  "$BACKUP_DIR/home_agent.zip"

# ── Phase 4: Done ─────────────────────────────────────────────────────────────
SIZE=$(du -sh "$FINAL_ZIP" | cut -f1)
echo ""
echo "[4/4] Backup complete."
echo "  Archive : $FINAL_ZIP"
echo "  Size    : $SIZE"
echo ""
if [ -d "/tmp/agent_memory_backups_${TIMESTAMP}" ]; then
  echo "  Memory snapshots at : /tmp/agent_memory_backups_${TIMESTAMP}"
  echo "  To restore          : mv /tmp/agent_memory_backups_${TIMESTAMP} /agent/memory/backups"
fi
echo ""
echo "  To rebuild long_term_memory.mv2 after restore:"
echo "    cd /agent && uv run python scripts/memory_ingest.py"

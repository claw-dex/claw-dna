#!/bin/bash
set -uo pipefail
# log_cleanup.sh — Archives old cycle logs to save space
# Constitution allows: "Log files older than 50 cycles may be compressed or summarised"
# Usage: ./log_cleanup.sh [--keep N] [--dry-run]
# Default: keeps last 50 cycles, archives older ones into /agent/memory/logs/archive/

KEEP=50
DRY_RUN=false
LOGS_DIR="/agent/memory/logs"
ARCHIVE_DIR="$LOGS_DIR/archive"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep) KEEP="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help)
      echo "Usage: $0 [--keep N] [--dry-run]"
      echo "  --keep N     Keep last N cycles (default: 50)"
      echo "  --dry-run    Show what would be done without doing it"
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Get current cycle number from state.json
CURRENT_CYCLE=$(python3 -c "
import json
s = json.load(open('/agent/memory/state.json'))
print(s.get('cycle_number', 0))
" 2>/dev/null)

if [ -z "$CURRENT_CYCLE" ] || [ "$CURRENT_CYCLE" -eq 0 ]; then
  echo "ERROR: Could not determine current cycle number"
  exit 1
fi

CUTOFF=$((CURRENT_CYCLE - KEEP))

if [ "$CUTOFF" -le 0 ]; then
  echo "Nothing to archive. Current cycle: $CURRENT_CYCLE, keeping last $KEEP cycles."
  echo "Archiving starts when cycle > $KEEP."
  exit 0
fi

echo "=== Log Cleanup ==="
echo "Current cycle: $CURRENT_CYCLE"
echo "Keep last:     $KEEP cycles"
echo "Archive:       cycles 1 through $CUTOFF"
echo "Dry run:       $DRY_RUN"
echo ""

# Find files to archive
FILES_TO_ARCHIVE=()
TOTAL_SIZE=0

for file in "$LOGS_DIR"/cycle-*; do
  [ -f "$file" ] || continue
  basename=$(basename "$file")
  # Extract cycle number from filename (cycle-N.log, cycle-N-prompt.md, cycle-N-system.md)
  cycle_num=$(echo "$basename" | sed -E 's/^cycle-([0-9]+).*/\1/')

  if [ -n "$cycle_num" ] && [ "$cycle_num" -le "$CUTOFF" ]; then
    size=$(stat -c%s "$file" 2>/dev/null || echo 0)
    TOTAL_SIZE=$((TOTAL_SIZE + size))
    FILES_TO_ARCHIVE+=("$file")
  fi
done

FILE_COUNT=${#FILES_TO_ARCHIVE[@]}

if [ "$FILE_COUNT" -eq 0 ]; then
  echo "No files to archive."
  exit 0
fi

HUMAN_SIZE=$(numfmt --to=iec "$TOTAL_SIZE" 2>/dev/null || echo "${TOTAL_SIZE}B")
echo "Files to archive: $FILE_COUNT ($HUMAN_SIZE)"
echo ""

if [ "$DRY_RUN" = true ]; then
  echo "--- Dry Run (no changes) ---"
  for file in "${FILES_TO_ARCHIVE[@]}"; do
    echo "  Would archive: $(basename "$file")"
  done
  exit 0
fi

# Create archive directory
mkdir -p "$ARCHIVE_DIR"

# Create tar.gz archive
ARCHIVE_NAME="cycles-1-to-${CUTOFF}.tar.gz"
ARCHIVE_PATH="$ARCHIVE_DIR/$ARCHIVE_NAME"

# Get just the basenames for tar
BASENAMES=()
for file in "${FILES_TO_ARCHIVE[@]}"; do
  BASENAMES+=("$(basename "$file")")
done

# Create archive from logs directory
cd "$LOGS_DIR"
tar -czf "$ARCHIVE_PATH" "${BASENAMES[@]}" 2>/dev/null

if [ $? -eq 0 ]; then
  ARCHIVE_SIZE=$(stat -c%s "$ARCHIVE_PATH" 2>/dev/null || echo 0)
  ARCHIVE_HUMAN=$(numfmt --to=iec "$ARCHIVE_SIZE" 2>/dev/null || echo "${ARCHIVE_SIZE}B")
  echo "Created archive: $ARCHIVE_NAME ($ARCHIVE_HUMAN)"

  # Remove archived files
  for file in "${FILES_TO_ARCHIVE[@]}"; do
    rm -f "$file"
  done
  echo "Removed $FILE_COUNT original files"

  SAVED=$((TOTAL_SIZE - ARCHIVE_SIZE))
  SAVED_HUMAN=$(numfmt --to=iec "$SAVED" 2>/dev/null || echo "${SAVED}B")
  echo "Space saved: $SAVED_HUMAN"
else
  echo "ERROR: Failed to create archive"
  exit 1
fi

echo ""
echo "Done. Archive at: $ARCHIVE_PATH"

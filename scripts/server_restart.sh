#!/bin/bash
set -euo pipefail
# server_restart.sh — Trigger Streamlit hot-reload
#
# Streamlit (managed by bootstrap.sh) watches Python files and reloads
# automatically when they change (runOnSave=true in .streamlit/config.toml).
# Touching server.py is the standard way to force a reload.
#
# Usage: ./server_restart.sh [--verify]

VERIFY=false
[ "$1" = "--verify" ] && VERIFY=true

echo "=== Server Reload ==="
echo "Timestamp: $(date -Iseconds)"

# Trigger Streamlit hot-reload by touching the entry point
echo "Triggering Streamlit hot-reload (touching server.py)..."
touch /agent/server.py
sleep 3  # Streamlit typically reloads within 2-3 seconds

if $VERIFY; then
  echo ""
  echo "--- Verification ---"
  HEALTH=$(curl -s --max-time 5 http://localhost:8081/app/_stcore/health 2>/dev/null || echo "")
  if [ "$HEALTH" = "ok" ]; then
    echo "Health check: OK"
  else
    echo "Health check: FAILED (got: '$HEALTH')"
    exit 1
  fi
fi

echo ""
echo "Server reload complete."

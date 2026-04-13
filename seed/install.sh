#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  One-time seed install — run during Docker image build only.
#  Installs: agent-browser CLI, Google Cloud CLI, Google Workspace CLI
# ══════════════════════════════════════════════════════════════
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── agent-browser CLI ─────────────────────────────────────────
echo "==> Installing agent-browser CLI"
sudo npx playwright install-deps \
    && sudo npm install -g agent-browser \
    && agent-browser install

# ── Google Cloud CLI ──────────────────────────────────────────
echo "==> Installing Google Cloud CLI"
chmod +x "$SCRIPT_DIR/install_gcloud.sh"
"$SCRIPT_DIR/install_gcloud.sh"

# ── Google Workspace CLI ──────────────────────────────────────
echo "==> Installing Google Workspace CLI"
sudo npm install -g @googleworkspace/cli

# ── Memvid CLI ────────────────────────────────────────────────
echo "==> Installing Memvid CLI"
chmod +x "$SCRIPT_DIR/install_memvid.sh"
"$SCRIPT_DIR/install_memvid.sh"

echo "==> Seed install complete"

#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  One-time seed install — run during Docker image build only.
#  Installs: agent-browser CLI, Google Cloud CLI, Google Workspace CLI, GitHub CLI
# ══════════════════════════════════════════════════════════════
set -e
# Non-fatal installer errors are suppressed; results reported at the end

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── agent-browser CLI ─────────────────────────────────────────
echo "==> Installing agent-browser CLI"
# install-deps and npm install need root; agent-browser install must run as
# the agent user so Playwright browsers land in /home/agent/.cache (not /root/.cache)
ok_agent_browser=true
npx playwright install-deps     || ok_agent_browser=false
npm install -g agent-browser    || ok_agent_browser=false
su -c "agent-browser install" agent || ok_agent_browser=false

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "==> Installation summary:"
_status() { $1 && echo "OK" || echo "FAILED"; }
echo "    agent-browser       : $(_status $ok_agent_browser)"

#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  Seed install — run during Docker image build, and re-run inside a live
#  container by the claw-update-dna skill whenever this file changes.
#  Every step must therefore be idempotent.
#  Installs: agent-browser CLI, long-term memory deps (LanceDB + fastembed)
# ══════════════════════════════════════════════════════════════
set -e
# Non-fatal installer errors are suppressed; results reported at the end

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Run a command as the agent user when we're root, so caches and virtualenvs
# end up agent-owned rather than root-owned. Uses a login shell so the agent's
# PATH (and therefore uv) is picked up.
run_as_agent() {
    if [ "$(id -u)" -eq 0 ] && id agent >/dev/null 2>&1; then
        su - agent -c "$1"
    else
        bash -lc "$1"
    fi
}

# ── agent-browser CLI ─────────────────────────────────────────
echo "==> Installing agent-browser CLI"
# install-deps and npm install need root; agent-browser install must run as
# the agent user so Playwright browsers land in /home/agent/.cache (not /root/.cache)
ok_agent_browser=true
npx playwright install-deps     || ok_agent_browser=false
npm install -g agent-browser    || ok_agent_browser=false
su -c "agent-browser install" agent || ok_agent_browser=false

# ── GitHub CLI ────────────────────────────────────────────────
echo "==> Installing GitHub CLI"
chmod +x "$SCRIPT_DIR/install_gh.sh"
ok_gh=true
"$SCRIPT_DIR/install_gh.sh" || ok_gh=false

# ── Long-term memory (LanceDB + fastembed) ────────────────────
# Syncs the Python deps and pre-downloads the ~130 MB bge-small ONNX weights,
# so the first ingest doesn't pay for that download inside a heartbeat cycle.
# Runs as the agent user so /agent/.cache/fastembed is agent-owned.
echo ""
echo "==> Installing long-term memory dependencies"
memory_installer="${SCRIPT_DIR}/install_memory_deps.sh"
if [ ! -f "${PROJECT_ROOT}/pyproject.toml" ]; then
    # Image build can run before the project tree is copied in; the
    # claw-update-dna re-run inside the container will pick it up.
    echo "    project not present yet — skipping"
    memory_status="SKIPPED"
elif [ ! -f "${memory_installer}" ]; then
    echo "    ${memory_installer} not found — skipping"
    memory_status="SKIPPED"
elif run_as_agent "bash '${memory_installer}'"; then
    memory_status="OK"
else
    memory_status="FAILED"
fi

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "==> Installation summary:"
_status() { $1 && echo "OK" || echo "FAILED"; }
echo "    agent-browser       : $(_status $ok_agent_browser)"
echo "    github-cli          : $(_status $ok_gh)"
echo "    memory (lancedb)    : ${memory_status}"

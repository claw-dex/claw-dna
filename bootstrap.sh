#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  bootstrap.sh — Agent process manager & entrypoint (v1)
#
#  Manages 2 services: Caddy (gateway) and Streamlit (dashboard).
#
#  First run:  Starts Caddy (lightweight), polls for auth
#  Auth:       docker exec -it $CONTAINER_NAME /agent/agent.sh --auth-status
#              (authenticate via the agent CLI's built-in flow)
#  After auth: Bootstrap auto-completes, no restart needed.
# ══════════════════════════════════════════════════════════════

set -euo pipefail

# Container name — overridable via `docker run -e CONTAINER_NAME=…`.
# Used in user-facing help text so copy-pasted docker commands match the
# name the operator actually chose.
CONTAINER_NAME="${CONTAINER_NAME:-myagent}"

# ── Colors ───────────────────────────────────────────────────
CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

step()    { echo -e "\n${BOLD}${CYAN}[$1/4]${NC} ${BOLD}$2${NC}"; echo -e "${DIM}$(printf '%.0s─' {1..50})${NC}"; }
success() { echo -e "  ${GREEN}✔${NC} $1"; }
fail()    { echo -e "  ${RED}✘${NC} $1"; }
info()    { echo -e "  ${DIM}→${NC} $1"; }
warn()    { echo -e "  ${YELLOW}⚠${NC} $1"; }

banner() {
    echo -e "${CYAN}"
    cat << 'EOF'
    ╔═══════════════════════════════════════════════════╗
    ║                                                   ║
    ║     █████╗  ██████╗ ███████╗███╗   ██╗████████╗   ║
    ║    ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝   ║
    ║    ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║      ║
    ║    ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║      ║
    ║    ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║      ║
    ║    ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝      ║
    ║                                                   ║
    ║       claw-dex/claw-dna - v1/base - 1.0.0         ║
    ║                                                   ║
    ╚═══════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
}

# Helper: check if the AI coding agent is authenticated (non-interactive, safe)
is_authenticated() {
    /agent/agent.sh --auth-status 2>/dev/null
}

# ── Service management functions ──────────────────────────────

CADDY_PID=""
STREAMLIT_PID=""
REAPPLY_PID=""

# Kill any stale processes holding a port before restarting a service.
# This prevents "port not available" loops when the wrapper PID (uv) exits
# but its child (the actual streamlit process) keeps the port open.
kill_stale_streamlit() {
    local pids
    pids=$(pgrep -f "streamlit.*server\.py" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "$pids" | xargs -r kill 2>/dev/null || true
        sleep 1
    fi
}

start_caddy() {
    echo -e "${DIM}[$(date -Is)] Starting Caddy gateway on port 8080...${NC}"
    caddy run --config /agent/Caddyfile --adapter caddyfile &
    CADDY_PID=$!
}

start_caddy_setup() {
    echo -e "${DIM}[$(date -Is)] Starting Caddy gateway (setup mode) on port 8080...${NC}"
    caddy run --config /agent/Caddyfile.setup --adapter caddyfile &
    CADDY_PID=$!
}

reload_caddy_production() {
    echo -e "${DIM}[$(date -Is)] Switching Caddy to production config...${NC}"
    caddy reload --config /agent/Caddyfile --adapter caddyfile 2>&1 || {
        warn "Caddy reload failed — restarting with production config"
        kill $CADDY_PID 2>/dev/null
        wait $CADDY_PID 2>/dev/null || true
        start_caddy
    }
}

start_streamlit() {
    kill_stale_streamlit
    echo -e "${DIM}[$(date -Is)] Starting Streamlit on port 8081...${NC}"
    cd /agent && uv run streamlit run server.py --server.baseUrlPath /app/ --server.enableCORS=false --server.enableXsrfProtection=false &
    STREAMLIT_PID=$!
}

_reapply_portal_auth() {
    local ready=false
    for i in 1 2 3 4 5; do
        if curl -sf http://localhost:2019/config/ >/dev/null 2>&1; then
            ready=true
            break
        fi
        sleep 1
    done
    if [ "$ready" = false ]; then
        echo -e "${YELLOW}[$(date -Is)] Caddy admin API not ready after 5s — skipping auth reapply${NC}"
        return
    fi
    cd /agent && uv run python scripts/portal_config.py auth --reapply 2>&1 | \
        while read -r line; do echo -e "${DIM}[$(date -Is)] $line${NC}"; done
}

start_services() {
    # Skip Caddy if already running (e.g. started during auth-wait phase)
    if [ -n "$CADDY_PID" ] && kill -0 "$CADDY_PID" 2>/dev/null; then
        echo -e "${DIM}[$(date -Is)] Caddy already running (PID=$CADDY_PID)${NC}"
    else
        start_caddy
    fi
    start_streamlit
    # Delay portal-auth reapply until after Streamlit is up to avoid CPU contention
    {
        sleep 4  # give Streamlit time to start
        _reapply_portal_auth
    } &
    REAPPLY_PID=$!
    echo -e "${DIM}[$(date -Is)] All services started (Caddy=$CADDY_PID, Streamlit=$STREAMLIT_PID)${NC}"
}

cleanup() {
    echo -e "\n${DIM}[$(date -Is)] Shutting down services...${NC}"
    kill $CADDY_PID $STREAMLIT_PID $REAPPLY_PID 2>/dev/null
    # Also kill any child processes (uv spawns streamlit as a child)
    kill_stale_streamlit
    wait
    exit 0
}

trap cleanup SIGTERM SIGINT

# Check if Streamlit is actually alive (not just the uv wrapper PID).
# uv exits after spawning streamlit, so we check for the real process.
is_streamlit_alive() {
    pgrep -f "streamlit.*server\.py" >/dev/null 2>&1
}

watchdog_loop() {
    echo -e "${DIM}[$(date -Is)] Watchdog active — monitoring services every 10s${NC}"
    while true; do
        sleep 10 &
        wait $! || true
        if ! kill -0 $CADDY_PID 2>/dev/null; then
            warn "Caddy crashed — restarting..."
            start_caddy
            _reapply_portal_auth &
        fi
        if ! is_streamlit_alive; then
            warn "Streamlit crashed — restarting..."
            start_streamlit
        fi
    done
}

# ══════════════════════════════════════════════════════════════
#  Quick-start: if already bootstrapped, start all services
# ══════════════════════════════════════════════════════════════

if [ -f /agent/memory/bootstrap.json ]; then
    if is_authenticated; then
        echo -e "${DIM}[$(date -Is)] Agent bootstrapped. Starting all services...${NC}"
    else
        echo -e "${YELLOW}[$(date -Is)] Auth expired. Re-authentication required.${NC}"
        echo -e "${YELLOW}Please authenticate the AI coding agent CLI.${NC}"
    fi
    start_services
    watchdog_loop
fi

# ══════════════════════════════════════════════════════════════
#  First boot — full bootstrap sequence
# ══════════════════════════════════════════════════════════════

banner
echo -e "${DIM}Starting bootstrap sequence...${NC}\n"

# ── Step 1: Verify directory structure ───────────────────────
step 1 "Verifying agent filesystem"

DIRS=("/agent/memory" "/agent/memory/logs" "/agent/messages" "/agent/web" "/agent/workspace" "/agent/prompts")
for dir in "${DIRS[@]}"; do
    mkdir -p "$dir"
    success "Verified $dir"
done

# Ensure message queues exist
[ -f /agent/messages/inbox.json ]  || echo '[]' > /agent/messages/inbox.json
[ -f /agent/messages/outbox.json ] || echo '[]' > /agent/messages/outbox.json
[ -f /agent/memory/goal.json ] || echo '[]' > /agent/memory/goal.json
success "Command queues ready"

# Verify constitution
if [ -f /agent/constitution.md ]; then
    success "Constitution found ($(wc -l < /agent/constitution.md) lines)"
else
    warn "No constitution.md — agent will run without guardrails"
fi

# Verify prompts
for prompt in bootstrap self-heal goal evolve; do
    if [ -f "/agent/prompts/${prompt}.md" ]; then
        success "Prompt: ${prompt}.md"
    else
        fail "Missing: /agent/prompts/${prompt}.md"
    fi
done

# ── Step 2: AI Coding Agent Authentication ───────────────────
step 2 "Authenticating AI coding agent"

if [ ! -x /agent/agent.sh ]; then
    fail "Agent wrapper script not found at /agent/agent.sh"
    exit 1
fi

AGENT_VERSION=$(/agent/agent.sh --version 2>&1 | head -1)
if [ $? -ne 0 ] || [ -z "$AGENT_VERSION" ]; then
    fail "AI coding agent CLI not found or not installed"
    echo -e "\n${RED}PATH: $PATH${NC}"
    exit 1
fi

success "AI coding agent installed: $AGENT_VERSION"

info "Checking existing authentication..."
if is_authenticated; then
    success "Already authenticated"
else
    warn "Not authenticated yet"
    echo ""
    echo -e "  ${YELLOW}╔═══════════════════════════════════════════════════════╗${NC}"
    echo -e "  ${YELLOW}║  Authentication required — run from another terminal: ║${NC}"
    echo -e "  ${YELLOW}╠═══════════════════════════════════════════════════════╣${NC}"
    echo -e "  ${YELLOW}║                                                       ║${NC}"
    echo -e "  ${YELLOW}║${NC}  ${BOLD}docker exec -it ${CONTAINER_NAME} claude${NC}                       ${YELLOW}║${NC}"
    echo -e "  ${YELLOW}║                                                       ║${NC}"
    echo -e "  ${YELLOW}╚═══════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${DIM}Starting Caddy gateway while waiting for auth...${NC}"
    echo -e "  ${DIM}(lightweight — Streamlit starts after authentication)${NC}"
    echo ""

    # Render setup.html with the current container name so the shown
    # `docker exec` command matches what the operator ran.
    sed -i "s|__CONTAINER_NAME__|${CONTAINER_NAME}|g" /agent/setup.html

    # Start Caddy in setup mode — serves setup.html at / while waiting for auth
    start_caddy_setup

    # Poll until auth appears (interruptible sleep to handle SIGTERM under set -e)
    info "Waiting for authentication (polling every 10s)..."
    while ! is_authenticated; do
        sleep 10 &
        wait $! || true
    done

    success "Authentication detected!"

    # Switch Caddy from setup page to production config (redirect / → /web/)
    reload_caddy_production

    # Caddy stays running — it will be reused by start_services later
fi

# ── Step 3: Verify web server ─────────────────────────────────
step 3 "Verifying web server"

if [ -f /agent/server.py ]; then
    success "server.py found ($(wc -l < /agent/server.py) lines)"

    # Batch syntax check — single Python process for all files
    SYNTAX_ERRORS=$(uv run python -c "
import py_compile, glob, sys
errors = []
for f in ['/agent/server.py'] + sorted(glob.glob('/agent/app/*.py')):
    try:
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
        errors.append(str(e))
if errors:
    print('\n'.join(errors), file=sys.stderr)
    sys.exit(1)
" 2>&1) || {
        fail "Syntax errors found:"
        echo "$SYNTAX_ERRORS"
        exit 1
    }
    success "server.py + app/ package syntax OK"
else
    fail "server.py not found — image may be built incorrectly"
    exit 1
fi

# ── Step 4: Finalize ─────────────────────────────────────────
step 4 "Finalizing bootstrap"

cat > /agent/memory/bootstrap.json << BJSON
{
    "completed_at": "$(date -Is)",
    "agent_cli_version": "$(/agent/agent.sh --version 2>&1 | head -1)",
    "python_version": "$(uv run python --version 2>&1)",
    "hostname": "$(hostname)"
}
BJSON
success "Bootstrap metadata saved"

# ── Start all services with watchdog (PID 1 is this script) ───
echo -e "${DIM}[$(date -Is)] Starting all services...${NC}"
start_services

# ── Done ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  Bootstrap complete! Agent is ready.${NC}"
echo -e "${GREEN}${BOLD}════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo ""
echo -e "  ${CYAN}1.${NC} Portal: ${BOLD}http://localhost:8080/app/${NC}"
echo ""
echo -e "  ${CYAN}2.${NC} Commit authenticated state ${DIM}(from another terminal):${NC}"
echo -e "     ${BOLD}docker commit ${CONTAINER_NAME} ${CONTAINER_NAME}:authenticated${NC}"
echo ""
echo -e "  ${CYAN}3.${NC} Open the portal and enter your first goal:"
echo -e "     ${BOLD}http://localhost:8080/app/${NC}"
echo -e "     ${DIM}The Streamlit UI will trigger the first heartbeat automatically.${NC}"
echo ""
echo -e "  ${CYAN}4.${NC} Or start the orchestrator:"
echo -e "     ${BOLD}./orchestrator.sh${NC}"
echo ""
echo -e "${DIM}Logs: docker logs -f ${CONTAINER_NAME}${NC}"
echo ""
# Run the watchdog loop to monitor services and restart if they crash (loops indefinitely)
watchdog_loop

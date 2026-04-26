#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  heartbeat.sh — One cycle of agent consciousness
#
#  Called externally via: docker exec [container-name] /agent/heartbeat.sh
#
#  Prompt hierarchy:
#    1. First cycle (no state)          → BOOTSTRAP prompt (build portal)
#    2. Portal unhealthy                → SELF-HEAL prompt (fix portal)
#    3. User command in inbox           → GOAL prompt (do user's task)
#    4. Active goal in progress         → GOAL prompt (continue working)
#    5. No idle-cycle in last 5 cycles  → IDLE prompt (prevent starvation)
#    6. Last N idle-cycles in a row     → SKIP cycle (prevent idle loop)
#    7. Otherwise                       → IDLE prompt (self-improve / consolidate)
#
#  The "idle" prompt is EVOLVE by default; with --agent-sleep it becomes DREAM
#  (nightly reflection/consolidation, see prompts/dream.md) only between
#  20:00 and 08:00 in the user's timezone — during the day the idle prompt
#  stays EVOLVE even when --agent-sleep is set. The consecutive cap is
#  --max-evolve (default 5) for evolve and --max-dream (default 5) for dream.
#
#  Uses:
#    --system-prompt         → fixed context (constitution, memory, container info)
#    -p                      → cycle-specific task prompt
# ══════════════════════════════════════════════════════════════

set -uo pipefail

# ── Prevent concurrent heartbeats (flock guard) ────────────
LOCK_FILE="/agent/memory/.heartbeat.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date -Is)] Another heartbeat is already running. Skipping."
    exit 0
fi
# Lock is held for the duration of the script via fd 9

# ── Parse arguments ──────────────────────────────────────────
AGENT_SLEEP=false
MAX_EVOLVE=5
MAX_DREAM=5
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-sleep) AGENT_SLEEP=true; shift ;;
        --max-evolve) MAX_EVOLVE="$2"; shift 2 ;;
        --max-dream) MAX_DREAM="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# IDLE_MODE is set after USER_TZ is read below, since the dream window
# (20:00–08:00) is evaluated in the user's local timezone.

# ── Guard: kill stale agent process from a previous crashed heartbeat ──
# If a previous heartbeat was killed (e.g., OOM, signal) without cleanup,
# a leftover agent process can still be running. Detect via the cycle lock
# file (written at cycle start, cleaned up on exit). If the PID in the lock
# is still alive but the heartbeat that spawned it is gone, kill it so this
# heartbeat can run cleanly — otherwise the leftover process keeps writing
# to cycles.json while we start a new cycle, causing overlapping entries.
CYCLE_LOCK="/agent/memory/.cycle.lock"
if [ -f "$CYCLE_LOCK" ]; then
    STALE_PID=$(jq -r '.pid // 0' "$CYCLE_LOCK" 2>/dev/null || echo 0)
    if [ "$STALE_PID" -gt 0 ] && [ "$STALE_PID" != "$$" ]; then
        if kill -0 "$STALE_PID" 2>/dev/null; then
            echo "[$(date -Is)] Stale cycle process (PID $STALE_PID) still running from previous heartbeat. Killing..."
            kill "$STALE_PID" 2>/dev/null || true
            sleep 2
            # Kill children BEFORE SIGKILL — after SIGKILL children reparent to PID 1
            pkill -P "$STALE_PID" 2>/dev/null || true
            kill -9 "$STALE_PID" 2>/dev/null || true
        fi
        rm -f "$CYCLE_LOCK"
    fi
fi

TIMESTAMP=$(date -Is)
CYCLE_NUM=$(jq -r '.cycle_number // 0' /agent/memory/state.json 2>/dev/null || echo 0)

# ── Read user timezone (default: UTC) ──
USER_TZ="UTC"
if [ -f /agent/memory/portal_config.json ]; then
    _tz=$(jq -r '.timezone // ""' /agent/memory/portal_config.json 2>/dev/null)
    [ -n "$_tz" ] && [ "$_tz" != "null" ] && USER_TZ="$_tz"
fi
USER_TIME=$(TZ="$USER_TZ" date "+%Y-%m-%d %H:%M:%S %Z")

# ── Idle mode selection (dream only at night when --agent-sleep is set) ──
# Dream window: 20:00–08:00 in the user's timezone. Outside that window, even
# with --agent-sleep on, the idle prompt falls back to evolve.
if $AGENT_SLEEP; then
    CURRENT_HOUR=$(TZ="$USER_TZ" date "+%H")
    CURRENT_HOUR=${CURRENT_HOUR#0}  # strip leading zero for arithmetic
    : "${CURRENT_HOUR:=0}"
    if [ "$CURRENT_HOUR" -ge 20 ] || [ "$CURRENT_HOUR" -lt 8 ]; then
        IDLE_MODE="dream"
        MAX_CONSECUTIVE_IDLE="$MAX_DREAM"
    else
        IDLE_MODE="evolve"
        MAX_CONSECUTIVE_IDLE="$MAX_EVOLVE"
    fi
else
    IDLE_MODE="evolve"
    MAX_CONSECUTIVE_IDLE="$MAX_EVOLVE"
fi

# ── Update last_heartbeat in state.json (fires every invocation, even during sleep) ──
HEARTBEAT_TS=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
TMP_STATE=$(mktemp /agent/memory/state.json.XXXXXX)
jq --arg ts "$HEARTBEAT_TS" '.last_heartbeat = $ts' /agent/memory/state.json > "$TMP_STATE" 2>/dev/null && mv "$TMP_STATE" /agent/memory/state.json || rm -f "$TMP_STATE"

CYCLE_NUM=$((CYCLE_NUM + 1))

# ── Cycle lock file (tracks active cycle PID for crash detection) ──
# Initially written with shell PID; updated with agent PID after launch.
CYCLE_LOCK="/agent/memory/.cycle.lock"
echo "{\"pid\": $$, \"cycle\": ${CYCLE_NUM}, \"started\": \"${TIMESTAMP}\"}" > "$CYCLE_LOCK"
cleanup_cycle_lock() { rm -f "$CYCLE_LOCK"; }
trap cleanup_cycle_lock EXIT

echo "[$TIMESTAMP] ════════ Cycle #${CYCLE_NUM} ════════"

# ── Scheduled Tasks (fallback: scheduler_daemon owns this normally; this
#    in-line check ensures reminders still fire if the daemon is down) ──
if [ -f /agent/memory/scheduled_tasks.json ]; then
    uv run python /agent/scripts/scheduler.py --check 2>/dev/null || true
fi

# ── Auto-start services (ensure services with auto_start:true are running) ──
uv run python /agent/scripts/service_manager.py auto-start 2>/dev/null || true

# ── Prompt Selection ─────────────────────────────────────────

select_prompt() {

    # 1. First boot — no state.json exists
    if [ ! -f /agent/memory/state.json ]; then
        echo "bootstrap"
        return
    fi

    # 2. Bootstrap check: cycle_number == 0 means agent hasn't run yet
    #    (must come before health check — fresh agent should bootstrap, not heal)
    local cycle_num
    cycle_num=$(jq -r '.cycle_number // 0' /agent/memory/state.json 2>/dev/null || echo 0)
    if [ "$cycle_num" -eq 0 ]; then
        echo "bootstrap"
        return
    fi

    # 3. Health check: Streamlit on port 8081 exposes /_stcore/health → returns "ok" when ready
    #    Retry up to 3 times with 3s gaps — Streamlit can be briefly unresponsive during hot-reload
    local health_resp=""
    local attempt
    for attempt in 1 2 3; do
        health_resp=$(curl -s --max-time 5 http://localhost:8081/app/_stcore/health 2>/dev/null || echo "")
        if [ "$health_resp" = "ok" ]; then
            break
        fi
        [ "$attempt" -lt 3 ] && sleep 3
    done
    if [ "$health_resp" != "ok" ]; then
        echo "heal:server_down"
        return
    fi

    # 3b. App render check — catches syntax/import/runtime errors that _stcore/health misses
    local app_check_exit
    timeout 45 uv run python /agent/scripts/app_check.py --json >/dev/null 2>&1
    app_check_exit=$?
    if [ "$app_check_exit" -eq 1 ]; then
        echo "heal:app_error"
        return
    fi
    # exit 2 (timeout) or 3 (unavailable) → non-fatal, continue

    # 4. User command waiting in inbox or active goal in progress
    #    (checked before starvation guard so active work always takes priority)
    local inbox_size
    inbox_size=$(jq 'length' /agent/messages/inbox.json 2>/dev/null || echo 0)
    if [ "$inbox_size" -gt 0 ]; then
        echo "goal"
        return
    fi

    local active_goals
    active_goals=$(jq '[.[] | select(.status == "pending" or .status == "in-progress" or .status == "in_progress")] | length' \
        /agent/memory/goal.json 2>/dev/null || echo 0)
    if [ "$active_goals" -gt 0 ]; then
        echo "goal"
        return
    fi

    # 5. Force the idle prompt if none in the last 5 cycles (prevents starvation
    #    when idle). IDLE_MODE = "evolve" normally, "dream" when --agent-sleep.
    local cycles_since_idle
    cycles_since_idle=$(jq --arg m "$IDLE_MODE" '
        [.[] | select(.type == $m)] | last | .cycle // 0
    ' /agent/memory/cycles.json 2>/dev/null || echo 0)
    local current_cycle
    current_cycle=$(jq -r '.cycle_number // 0' /agent/memory/state.json 2>/dev/null || echo 0)
    local gap=$(( current_cycle - cycles_since_idle ))
    if [ "$gap" -ge 5 ]; then
        echo "$IDLE_MODE"
        return
    fi

    # 6. All clear — run the idle prompt (skip if consecutive idle limit reached).
    local all_idle
    all_idle=$(jq -r --argjson n "$MAX_CONSECUTIVE_IDLE" --arg m "$IDLE_MODE" '
        if length < $n then false
        else (. | reverse | .[0:$n] | all(.type == $m))
        end
    ' /agent/memory/cycles.json 2>/dev/null || echo false)
    if [ "$all_idle" = "true" ]; then
        echo "skip"
        return
    fi
    echo "$IDLE_MODE"
}

PROMPT_MODE=$(select_prompt)
echo "[$TIMESTAMP] Prompt mode: ${PROMPT_MODE}"

# ── Skip mode: consecutive evolve limit reached ──────────────
if [ "$PROMPT_MODE" = "skip" ]; then
    echo "[$TIMESTAMP] Last ${MAX_CONSECUTIVE_IDLE} cycles were all ${IDLE_MODE}. Skipping cycle."
    exit 0
fi

# ── System Prompt (fixed context — same every cycle) ─────────

build_system_prompt() {
    cat <<SYSTEM
<agent_system_prompt>
$(cat /agent/system.md 2>/dev/null)
</agent_system_prompt>

<agent_constitution>
$(cat /agent/constitution.md 2>/dev/null || echo "No constitution found.")
</agent_constitution>
SYSTEM

    # Inject public hostname if configured
    if [ -f /agent/memory/portal_config.json ]; then
        local public_url
        public_url=$(jq -r '.public_url // ""' /agent/memory/portal_config.json 2>/dev/null)
        if [ -n "$public_url" ]; then
            cat <<PUBLIC

<public_url>
This agent is accessible at: ${public_url}

When sharing links with the user (portal, file explorer, workspace files, generated reports), use this public URL as the base instead of localhost:8080. For example:
- Portal: ${public_url}/app/
- Static Web: ${public_url}/web/ (static files from /agent/web/)
- File Explorer: ${public_url}/_/
- Workspace files: ${public_url}/_/agent/workspace/path/to/<filename>

Note: For internal operations (curl, health checks, Caddy admin API), continue using localhost.
</public_url>
PUBLIC
        fi
    fi
}

# ── Task Prompt (mode-specific — changes each cycle) ─────────

build_task_prompt() {
    local mode="$1"

    echo "This is cycle #${CYCLE_NUM}. Current date and time: ${USER_TIME} (${USER_TZ})."
    echo ""
    echo "CRITICAL: ONE cycle per heartbeat. Run cycle_start.py exactly once at the start"
    echo "and cycle_close.py exactly once at the end. Never create additional cycle entries"
    echo "in cycles.json. If you discover new goals or inbox items, leave them for the next"
    echo "heartbeat. Overlapping cycles cause interruptions and lost work."
    echo ""

    case "$mode" in
        bootstrap)
            cat /agent/prompts/bootstrap.md
            echo ""
            echo "<your_goals>"
            cat /agent/memory/goal.json 2>/dev/null || echo '[]'
            echo "</your_goals>"
            echo "Read this goal carefully. Incorporate it into your bootstrap plan."
            echo "You MAY modify server.py and app/*.py to customise the Streamlit UI for this goal."
            ;;
        heal:*)
            local symptom="${mode#heal:}"
            cat /agent/prompts/self-heal.md
            echo ""
            echo "## Detected Symptom"
            echo "Health check result: \`${symptom}\`"
            echo ""
            case "$symptom" in
                app_error)
                    echo "The Streamlit server process is running but server.py raised an exception during headless render."
                    echo "This means a broken import, missing dependency, or error in init."
                    echo ""
                    echo "App check result:"
                    echo '```json'
                    cat /agent/memory/app_check_result.json 2>/dev/null || echo "{}"
                    echo '```'
                    echo ""
                    echo "Reproduce: cd /agent && uv run python scripts/app_check.py"
                    ;;
                *)
                    echo "Streamlit health endpoint (/_stcore/health) did not return 'ok'."
                    echo "Check that the Streamlit process is running on port 8081."
                    echo "The process manager (PID 1) runs Caddy (8080) and Streamlit (8081)."
                    ;;
            esac
            ;;
        goal)
            cat /agent/prompts/goal.md
            echo ""
            echo "<your_inbox_messages>"
            echo "(sorted by priority, 1=highest)"
            jq '[.[] | select(.type != "goal")] | sort_by(.priority // 3)' /agent/messages/inbox.json 2>/dev/null || echo '[]'
            echo "</your_inbox_messages>"
            echo ""
            echo "<new_goals_to_start>"
            echo "(sorted by priority, 1=highest)"
            jq '[.[] | select(.type == "goal")] | sort_by(.priority // 3)' /agent/messages/inbox.json 2>/dev/null || echo '[]'
            echo "</new_goals_to_start>"
            echo ""
            echo "<previous_unfinished_goals>"
            jq '[.[] | select(.status == "pending" or .status == "in-progress" or .status == "in_progress")] | sort_by(.created_at) | .[0:5]' /agent/memory/goal.json 2>/dev/null || echo '[]'
            echo "</previous_unfinished_goals>"
            ;;
        evolve)
            cat /agent/prompts/evolve.md
            echo ""
            echo "<your_current_goals>"
            echo "(active goals from goal.json — pending/in-progress)"
            jq '[.[] | select(.status == "pending" or .status == "in-progress" or .status == "in_progress")]' /agent/memory/goal.json 2>/dev/null || echo '[]'
            echo "</your_current_goals>"
            echo ""
            echo "<your_past_goals>"
            echo "(all completed/failed from goal.json + up to 20 most recent from goal_history.json, sorted by created_at)"
            jq -s '
                ((.[0] // []) | map(select(.status == "completed" or .status == "failed")))
                + ((.[1] // []) | sort_by(.created_at) | .[-20:])
                | sort_by(.created_at)
            ' /agent/memory/goal.json /agent/memory/goal_history.json 2>/dev/null || echo '[]'
            echo "</your_past_goals>"
            ;;
        dream)
            cat /agent/prompts/dream.md
            echo ""
            echo "<your_past_failed_goals>"
            echo "(all failed goals from goal.json + goal_history.json, sorted by created_at)"
            jq -s '
                (((.[0] // []) + (.[1] // []))
                 | map(select(.status == "failed"))
                 | sort_by(.created_at))
            ' /agent/memory/goal.json /agent/memory/goal_history.json 2>/dev/null || echo '[]'
            echo "</your_past_failed_goals>"
            ;;
    esac
}

SYSTEM_PROMPT=$(build_system_prompt)
TASK_PROMPT=$(build_task_prompt "$PROMPT_MODE")

# ── Save prompts for debugging ─────────────────────────────
echo "$SYSTEM_PROMPT" > "/agent/memory/logs/cycle-${CYCLE_NUM}-system.md"
echo "$TASK_PROMPT" > "/agent/memory/logs/cycle-${CYCLE_NUM}-prompt.md"

# ── Invoke AI Coding Agent ───────────────────────────────────
echo "[$TIMESTAMP] Invoking AI coding agent..."

# Session continuity: resume previous session for multi-cycle goals
SESSION_ID=""
if [ "$PROMPT_MODE" = "goal" ]; then
    SESSION_ID=$(jq -r '[.[] | select(.status == "in_progress")] | first | .session_id // ""' \
        /agent/memory/goal.json 2>/dev/null || echo "")
    if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "null" ] && [ "$SESSION_ID" != "" ]; then
        echo "[$TIMESTAMP] Resuming session: ${SESSION_ID}"
    fi
fi

# ── Update last_cycle_run in state.json (right before invoking the agent) ──
CYCLE_RUN_TS=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
TMP_STATE=$(mktemp /agent/memory/state.json.XXXXXX)
jq --arg ts "$CYCLE_RUN_TS" '.last_cycle_run = $ts' /agent/memory/state.json > "$TMP_STATE" 2>/dev/null && mv "$TMP_STATE" /agent/memory/state.json || rm -f "$TMP_STATE"

# ── Sync JSON memory → .md files for agent auto-memory ──
# Must run before the agent starts so auto-memory reflects current state.
uv run python /agent/scripts/memory_sync.py 2>/dev/null || true

cd /agent
# Run agent in background so we can capture its PID for crash detection.
# The lock file is updated with the agent PID so the stale-process guard
# (at the top of heartbeat.sh) can kill it if a previous heartbeat crashed.
RESUME_OPT=""
if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "null" ] && [ "$SESSION_ID" != "" ]; then
    RESUME_OPT="-r $SESSION_ID"
fi

/agent/agent.sh --yolo \
    --system-prompt-file "/agent/memory/logs/cycle-${CYCLE_NUM}-system.md" \
    --task-prompt-file "/agent/memory/logs/cycle-${CYCLE_NUM}-prompt.md" \
    $RESUME_OPT \
    --output-format text \
    2>&1 | tee "/agent/memory/logs/cycle-${CYCLE_NUM}.log" &
AGENT_PID=$!
echo "{\"pid\": ${AGENT_PID}, \"cycle\": ${CYCLE_NUM}, \"started\": \"${TIMESTAMP}\"}" > "$CYCLE_LOCK"
wait $AGENT_PID
EXIT_CODE=$?

# ── Archive transcript (preserve full reasoning chain before compaction) ──
TRANSCRIPT_DIR="/agent/memory/transcripts"
mkdir -p "$TRANSCRIPT_DIR"
AGENT_SESSION_DIR=$(/agent/agent.sh --get-transcript-dir 2>/dev/null || echo "")
if [ -n "$AGENT_SESSION_DIR" ] && [ -d "$AGENT_SESSION_DIR" ]; then
    LATEST_SESSION=$(ls -t "$AGENT_SESSION_DIR"/*.jsonl 2>/dev/null | head -1)
    if [ -n "$LATEST_SESSION" ]; then
        cp "$LATEST_SESSION" "${TRANSCRIPT_DIR}/cycle-${CYCLE_NUM}.jsonl" 2>/dev/null || true
        # Compress transcripts older than 7 days to save space
        find "$TRANSCRIPT_DIR" -name "*.jsonl" -mtime +7 -exec gzip -q {} \; 2>/dev/null || true
    fi
fi

echo "[$(date -Is)] Cycle #${CYCLE_NUM} complete (exit code: ${EXIT_CODE})."

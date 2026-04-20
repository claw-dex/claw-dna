#!/bin/bash

# ══════════════════════════════════════════════════════════════
#  agent.sh — Unified wrapper for AI coding agent CLIs
#
#  Currently supports: claude
#  Future: gemini, codex, and other AI coding agents
#
#  Usage: agent.sh [options] [task_prompt]
#
#  Options:
#    -u agent_user_name    Run as specified user
#    --yolo                Enable dangerous permissions mode
#    -r session_id         Resume session (use 'last' for most recent)
#    -s system_prompt      System prompt text
#    -p task_prompt         Task prompt text
#    --output-format fmt   Output format (text, json)
#    --auth-status         Check authentication status (exit 0=ok, 1=not)
#    --version             Print agent CLI version
#    --get-transcript-dir  Print session transcript directory path
#    -h, --help            Show usage
# ══════════════════════════════════════════════════════════════

set -e

# ── Agent backend selection ────────────────────────────────────
# Override with AI_AGENT_TYPE environment variable
AGENT_TYPE="${AI_AGENT_TYPE:-claude}"

# ── Usage ──────────────────────────────────────────────────────
show_usage() {
    echo "Usage: $0 [options] [task_prompt]"
    echo ""
    echo "Unified wrapper for AI coding agent CLIs."
    echo "Currently supports: claude (default)"
    echo ""
    echo "Options:"
    echo "  -u agent_user_name    Run as specified user"
    echo "  --yolo                Enable dangerous permissions mode (skips tool restrictions)"
    echo "  -r session_id         Resume a conversation with session ID (use 'last' for most recent)"
    echo "  -s system_prompt      System prompt text"
    echo "  -p task_prompt        Task prompt text"
    echo "  --output-format fmt   Output format (text, json, etc.)"
    echo "  --auth-status         Check if the agent CLI is authenticated (exit 0=ok, 1=not)"
    echo "  --version             Print the agent CLI version"
    echo "  --get-transcript-dir  Print session transcript directory path"
    echo "  -h, --help            Show this usage"
    echo ""
    echo "Environment:"
    echo "  AI_AGENT_TYPE         Agent backend to use (default: claude)"
    echo ""
    echo "Examples:"
    echo "  $0 --version"
    echo "  $0 --auth-status"
    echo "  $0 --yolo -p 'perform assigned tasks'"
    echo "  $0 -u agent --yolo -s 'You are an AI agent' -p 'do work'"
    echo "  $0 -r last"
    echo "  $0 -r abc123 -p 'continue with bug fixes'"
    exit 0
}

# ── Parse arguments ────────────────────────────────────────────
AGENT_USER=""
TASK_PROMPT=""
TASK_PROMPT_FILE=""
SYSTEM_PROMPT=""
YOLO_MODE=false
RESUME_SESSION=""
OUTPUT_FORMAT=""
ACTION=""  # auth-status, version, get-transcript-dir

while [[ $# -gt 0 ]]; do
    case $1 in
        -u)
            AGENT_USER="$2"
            shift 2
            ;;
        --yolo)
            YOLO_MODE=true
            shift
            ;;
        -r)
            RESUME_SESSION="$2"
            shift 2
            ;;
        -s|--system-prompt)
            SYSTEM_PROMPT="$2"
            shift 2
            ;;
        --system-prompt-file)
            SYSTEM_PROMPT="$(cat "$2" 2>/dev/null)"
            shift 2
            ;;
        -p)
            TASK_PROMPT="$2"
            shift 2
            ;;
        --task-prompt-file)
            TASK_PROMPT_FILE="$2"
            shift 2
            ;;
        --output-format)
            OUTPUT_FORMAT="$2"
            shift 2
            ;;
        --auth-status)
            ACTION="auth-status"
            shift
            ;;
        --version)
            ACTION="version"
            shift
            ;;
        --get-transcript-dir)
            ACTION="get-transcript-dir"
            shift
            ;;
        -h|--help)
            show_usage
            ;;
        *)
            # Remaining arguments treated as task prompt if -p not used
            if [ -z "$TASK_PROMPT" ]; then
                TASK_PROMPT="$*"
            fi
            break
            ;;
    esac
done

# ── Helper: run command as user (or directly) ─────────────────
run_cmd() {
    if [ -n "$AGENT_USER" ]; then
        runuser -u "$AGENT_USER" -- "$@"
    else
        "$@"
    fi
}

# ══════════════════════════════════════════════════════════════
#  Backend: Claude Code CLI
# ══════════════════════════════════════════════════════════════

claude_auth_status() {
    local status
    status=$(run_cmd claude auth status 2>/dev/null) || return 1
    echo "$status" | grep -q '"loggedIn": true'
}

claude_version() {
    run_cmd claude --version 2>&1 | head -1
}

claude_get_transcript_dir() {
    local session_dir
    session_dir="$HOME/.claude/projects/$(pwd | tr '/' '-')"
    echo "$session_dir"
}

claude_run() {
    local cmd_args=()

    # Permissions mode
    if [ "$YOLO_MODE" = true ]; then
        cmd_args+=("--dangerously-skip-permissions")
    fi

    # System prompt with auto-append from claude-system-prompt.md
    local final_system_prompt="$SYSTEM_PROMPT"
    if [ -f "/home/agent/claude-system-prompt.md" ]; then
        local claude_system_content
        claude_system_content=$(cat /home/agent/claude-system-prompt.md)
        local claude_system_block="<claude_system_prompt>
${claude_system_content}
</claude_system_prompt>"
        if [ -n "$final_system_prompt" ]; then
            final_system_prompt="${final_system_prompt}

${claude_system_block}"
        else
            final_system_prompt="$claude_system_block"
        fi
    fi

    if [ -n "$final_system_prompt" ]; then
        cmd_args+=("--system-prompt" "$final_system_prompt")
    fi

    # Resume session
    if [ -n "$RESUME_SESSION" ]; then
        if [ "$RESUME_SESSION" = "last" ]; then
            cmd_args+=("--continue")
        else
            cmd_args+=("--resume" "$RESUME_SESSION")
        fi
    fi

    # Output format
    if [ -n "$OUTPUT_FORMAT" ]; then
        cmd_args+=("--output-format" "$OUTPUT_FORMAT")
    fi

    # Task prompt: prefer file redirection (avoids ARG_MAX), fall back to -p flag or no prompt
    # claude reads stdin as the user prompt when --output-format is set (non-interactive mode)
    if [ -n "$TASK_PROMPT_FILE" ]; then
        run_cmd claude "${cmd_args[@]}" < "$TASK_PROMPT_FILE"
    elif [ -n "$TASK_PROMPT" ]; then
        printf '%s' "$TASK_PROMPT" | run_cmd claude "${cmd_args[@]}"
    else
        run_cmd claude "${cmd_args[@]}"
    fi
}

# ══════════════════════════════════════════════════════════════
#  Backend: Gemini (stub — not yet implemented)
# ══════════════════════════════════════════════════════════════

gemini_auth_status() {
    echo "Gemini backend not yet implemented" >&2
    return 1
}

gemini_version() {
    echo "Gemini backend not yet implemented" >&2
    return 1
}

gemini_get_transcript_dir() {
    echo ""
}

gemini_run() {
    echo "Gemini backend not yet implemented" >&2
    return 1
}

# ══════════════════════════════════════════════════════════════
#  Backend: Codex (stub — not yet implemented)
# ══════════════════════════════════════════════════════════════

codex_auth_status() {
    echo "Codex backend not yet implemented" >&2
    return 1
}

codex_version() {
    echo "Codex backend not yet implemented" >&2
    return 1
}

codex_get_transcript_dir() {
    echo ""
}

codex_run() {
    echo "Codex backend not yet implemented" >&2
    return 1
}

# ══════════════════════════════════════════════════════════════
#  Dispatch to active backend
# ══════════════════════════════════════════════════════════════

dispatch() {
    local action="$1"
    case "$AGENT_TYPE" in
        claude)
            case "$action" in
                auth-status)         claude_auth_status ;;
                version)             claude_version ;;
                get-transcript-dir)  claude_get_transcript_dir ;;
                run)                 claude_run ;;
            esac
            ;;
        gemini)
            case "$action" in
                auth-status)         gemini_auth_status ;;
                version)             gemini_version ;;
                get-transcript-dir)  gemini_get_transcript_dir ;;
                run)                 gemini_run ;;
            esac
            ;;
        codex)
            case "$action" in
                auth-status)         codex_auth_status ;;
                version)             codex_version ;;
                get-transcript-dir)  codex_get_transcript_dir ;;
                run)                 codex_run ;;
            esac
            ;;
        *)
            echo "Unknown agent type: $AGENT_TYPE" >&2
            echo "Supported types: claude, gemini, codex" >&2
            exit 1
            ;;
    esac
}

# ── Execute ────────────────────────────────────────────────────

if [ -n "$ACTION" ]; then
    dispatch "$ACTION"
else
    # Default: run the agent
    if [ -z "$TASK_PROMPT" ] && [ -z "$RESUME_SESSION" ]; then
        TASK_PROMPT="perform assigned tasks"
    fi
    dispatch "run"
fi

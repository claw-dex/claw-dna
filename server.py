"""
claw-dex/claw-dna - v1/base - Streamlit Portal
Entry point for: uv run streamlit run server.py

Enum Reference: See prompts/enum.md for all valid status values and other enums.
"""

import traceback
from datetime import datetime, timezone

import hydralit_components as hc
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from app.shared import _startup_check, heartbeat_freshness
from app.data import load_state, load_errors, load_cycle_velocity, load_services
from app import (
    agents_tab,
    chat,
    commands_tab,
    memory_tab,
    workspace_tab,
    system_tab,
    overview_tab,
    credential_tab,
    emails_tab,
    glance,
    services_tab,
)

# ── Tab registry — add/remove tabs by editing this list only ─────────────────
# Each entry: (label, module, display_name_for_errors[, group])
# group defaults to "General" if omitted (backward compatible)
TAB_REGISTRY = [
    # Agent Console — operational tools
    ("🎛️ Command Center", commands_tab, "Command Center", "Agent Console"),
    ("📓 Memory", memory_tab, "Memory", "Agent Console"),
    ("🔭 Overview", overview_tab, "Overview", "Agent Console"),
    ("🤖 Agents", agents_tab, "Agents", "Agent Console"),
    # Core — file / credential / email / system management
    ("📁 Workspace", workspace_tab, "Workspace", "Core"),
    ("🔑 Credentials", credential_tab, "Credentials", "Core"),
    ("📧 Email", emails_tab, "Email", "Core"),
    ("⚙️ System", system_tab, "System", "Core"),
    ("🔧 Services & Cron", services_tab, "Services & Cron", "Core"),
]


def _group_tabs(registry, default_group="General"):
    """Organize TAB_REGISTRY entries into ordered groups."""
    groups = {}
    for entry in registry:
        group = entry[3] if len(entry) >= 4 else default_group
        groups.setdefault(group, []).append((entry[0], entry[1], entry[2]))
    return list(groups.items())


# ── Page config (must be first Streamlit call) ────────────────
st.set_page_config(
    page_title="Autonomous AI Agent",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ── Tab-level crash isolation ─────────────────────────────────
def _is_cache_key_error(exc: Exception) -> bool:
    """Return True if the exception is a Streamlit @st.cache_data LRU eviction KeyError.

    This happens when the in-memory LRU cache evicts data while the index still
    holds the hash key, causing a KeyError on the next read.  The error bubbles
    up through cachetools.__getitem__ → streamlit cache internals → module.render().
    """
    if not isinstance(exc, KeyError):
        return False
    # The key is always a hex MD5 hash string (32 hex chars)
    raw = exc.args[0] if exc.args else ""
    return (
        isinstance(raw, str)
        and len(raw) == 32
        and all(c in "0123456789abcdef" for c in raw)
    )


def _safe_render(module, tab_name: str):
    """Call module.render() inside a try/except so a single broken module
    cannot crash the entire portal. Shows an inline error instead.

    Special case: if the error is a Streamlit @st.cache_data LRU eviction
    KeyError (stale hash key after memory pressure), silently clear the cache
    and retry once — this is a transient framework bug, not an app bug.
    """
    try:
        module.render()
    except Exception as exc:
        # ── Transient Streamlit cache miss — clear & retry once ──────────
        if _is_cache_key_error(exc):
            try:
                st.cache_data.clear()
                module.render()
                return  # retry succeeded — no error shown, no logging
            except Exception as exc2:
                exc = exc2  # fall through to normal error handling

        st.error(f"**{tab_name} encountered an error.**")
        with st.expander("Error details (for debugging)"):
            st.code(traceback.format_exc(), language="python")
        st.warning(
            "The rest of the portal is unaffected. "
            "The agent will automatically attempt to fix this on the next cycle."
        )
        # Log to server_errors.json so the agent can detect it
        try:
            import json, os, time
            from app.shared import ERROR_LOG_PATH, _write_json_atomic

            errors = []
            if os.path.exists(ERROR_LOG_PATH):
                try:
                    with open(ERROR_LOG_PATH) as f:
                        errors = json.load(f)
                except (json.JSONDecodeError, OSError):
                    errors = []
            errors.append(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "tab": tab_name,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            # Keep last 20 errors only
            _write_json_atomic(ERROR_LOG_PATH, errors[-20:], indent=2)
        except Exception:
            pass  # never let error-logging crash the portal


# ── First-run setup screen ────────────────────────────────────
def _render_first_run():
    from app.data import (
        write_first_goal,
        trigger_bootstrap_heartbeat,
        load_goals,
        save_portal_config,
    )

    st.subheader("Welcome — First Run Setup")

    # Detect already-written goal (survives page refresh during bootstrap)
    goals = load_goals() or []
    already_started = len(goals) > 0

    if st.session_state.get("bootstrap_triggered") or already_started:
        goal_text = (
            goals[0].get("goal") or goals[0].get("content", "")
            if goals
            else st.session_state.get("first_goal", "")
        )
        st.success("Goal saved. The agent is bootstrapping...")
        if goal_text:
            st.write(f"**Your goal:** {goal_text}")
        st.info(
            "The agent is reading your goal and initialising. "
            "This page will update automatically when it is ready."
        )
        st.write("To start the automatic heartbeat, run from your terminal:")
        st.code("./orchestrator.sh", language="bash")
        st.caption("Auto-refresh every 60 seconds.")
    else:
        st.write(
            "Tell the agent what you would like it to work on. "
            "It will bootstrap itself with your goal in mind and may customise its interface accordingly."
        )
        with st.form("first_run_form"):
            first_goal = st.text_area(
                "Your first goal",
                placeholder="e.g. Monitor my stock portfolio, Help me learn Python, Build a web scraper…",
                height=150,
            )
            from zoneinfo import available_timezones

            tz_list = sorted(available_timezones())
            default_idx = tz_list.index("UTC") if "UTC" in tz_list else 0
            selected_tz = st.selectbox(
                "Your working timezone",
                options=tz_list,
                index=default_idx,
                help="The agent will use this to show dates/times in your local timezone.",
            )
            submitted = st.form_submit_button("Start Agent", type="primary")

        if submitted:
            if not first_goal or not first_goal.strip():
                st.error("Please enter a goal to get started.")
            else:
                ts = datetime.now(timezone.utc).isoformat()
                write_first_goal(first_goal.strip(), ts)
                save_portal_config("timezone", selected_tz)
                result = trigger_bootstrap_heartbeat()
                if result.get("ok"):
                    st.session_state["bootstrap_triggered"] = True
                    st.session_state["first_goal"] = first_goal.strip()
                    st.rerun()
                else:
                    st.error(f"Failed to start agent: {result.get('error')}")


# ── One-time initialization (runs once per server process) ────
@st.cache_resource
def _init():
    _startup_check()
    return True


# ── Splash screen on first session load only ──────────────────
if "app_initialized" not in st.session_state:
    with hc.HyLoader("Loading Agent…", hc.Loaders.standard_loaders, index=[3, 0, 5]):
        _init()
    st.session_state["app_initialized"] = True
else:
    _init()

# ── Auto-refresh (header metrics only — fixed 60s) ───────────────────────────
# The chat module owns its own faster poll-driven refresh in render() so the
# global timer cannot affect chat correctness. This keeps the 60s rerun from
# being entangled with the chat session: the ClaudeChat singleton lives in
# @st.cache_resource and survives reruns, so the global tick is harmless to
# the chat — it just refreshes header metrics (heartbeat, cycle, services).
st_autorefresh(interval=60_000, key="global_refresh")

# ── Header ────────────────────────────────────────────────────
state = load_state() or {}
cycle_num = state.get("cycle_number", 0)
agent_status = state.get("agent_status", "unknown")
last_heartbeat = state.get("last_heartbeat", "—")
current_goal = state.get("current_goal", "")

# Status pill color (see prompts/enum.md → Agent Status for all valid values)
status_colors = {
    "idle": "🟡",
    "running": "🟢",
    "healing": "🔴",
    "bootstrapping": "🔵",
    "awaiting_first_heartbeat": "⚪",
    "waiting_for_human": "🟠",  # Agent waiting for user input/escalation
}
status_icon = status_colors.get(agent_status, "⚪")


hb_display, hb_icon = heartbeat_freshness(last_heartbeat)
velocity = load_cycle_velocity()
_services = load_services() or {}
_alive_services = sum(1 for s in _services.values() if s.get("alive"))

st.title("Autonomous AI Agent")
st.caption("Autonomous AI agent — self-improving, self-healing, self-evolving.")

col_status, col_cycle, col_hb, col_vel, col_svc, col_goal, col_health = st.columns(
    [1, 1, 1, 1, 1, 3, 1]
)
with col_status:
    st.metric("Status", f"{status_icon} {agent_status}")
with col_cycle:
    st.metric("Cycle", f"#{cycle_num}")
with col_hb:
    st.metric("Heartbeat", f"{hb_icon} {hb_display}")
with col_vel:
    vel_str = f"{velocity}/hr" if velocity is not None else "—"
    st.metric(
        "Velocity", vel_str, help="Cycles per hour (rolling last 10 completed cycles)"
    )
with col_svc:
    _svc_icon = (
        "🟢"
        if _alive_services == len(_services) and _alive_services > 0
        else ("🟡" if _alive_services > 0 else "⚪")
    )
    st.metric(
        "Services",
        f"{_svc_icon} {_alive_services}/{len(_services)}",
        help="Running / total registered services",
    )
with col_goal:
    _goal_display = (current_goal or "—")[:60] + (
        "…" if current_goal and len(current_goal) > 60 else ""
    )
    _goal_help = current_goal if current_goal and len(current_goal) > 60 else None
    st.metric("Current Goal", _goal_display, help=_goal_help)
with col_health:
    try:
        from datetime import timedelta

        _errs = load_errors() or []
        _cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        _recent_errs = [e for e in _errs if e.get("timestamp", "") >= _cutoff]
        _err_count = len(_recent_errs)
        _health_icon = "🟢" if _err_count == 0 else ("🟡" if _err_count < 5 else "🔴")
        st.metric("Portal Health", f"{_health_icon} {_err_count} err")
        if _err_count > 0:
            st.caption("See System → Tab Crash Errors")
    except Exception:
        st.metric("Portal Health", "⚪ —")

st.divider()

# ── Main content — first-run gate ─────────────────────────────
if cycle_num == 0:
    _render_first_run()
else:
    # Quick-glance dashboard — Goals/Inbox/Outbox at a glance
    _safe_render(glance, "Quick Glance")

    # Chat — always visible at top of page
    _safe_render(chat, "Chat")

    st.divider()

    # Remaining tabs (grouped when multiple groups exist)
    grouped = _group_tabs(TAB_REGISTRY)

    if not grouped:
        st.info("No tabs configured.")
    elif len(grouped) == 1:
        # Single group → flat tabs, identical to original layout
        _, tabs_in_group = grouped[0]
        tab_objects = st.tabs([label for label, _, _ in tabs_in_group])
        for tab_obj, (_, module, name) in zip(tab_objects, tabs_in_group):
            with tab_obj:
                _safe_render(module, name)
    else:
        # Multiple groups → group selector + tabs
        group_names = [g for g, _ in grouped]
        selected = st.segmented_control(
            "Section",
            group_names,
            default=group_names[0],
            label_visibility="collapsed",
        )
        if selected is None:
            selected = group_names[0]
        tabs_in_group = next((t for g, t in grouped if g == selected), [])
        if len(tabs_in_group) > 12:
            st.caption(
                f"⚠️ {len(tabs_in_group)} tabs in this section — consider splitting."
            )
        if tabs_in_group:
            tab_objects = st.tabs([label for label, _, _ in tabs_in_group])
            for tab_obj, (_, module, name) in zip(tab_objects, tabs_in_group):
                with tab_obj:
                    _safe_render(module, name)

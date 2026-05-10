"""Tab: Agents — per-agent chat history, inbox, config, and operator actions.

Surfaces every entry in `/agent/memory/agents.json` (internal + external)
and lets the operator inspect or interact with one agent at a time:

* **💬 Chat** — for internal agents, the daemon's `chat_history.json`.
  The daemon writes the inbound user record as soon as a message arrives
  and appends the assistant record once the SDK turn finishes, so a
  user-only tail in the transcript is the expected mid-turn state — not
  a stuck or broken agent. The portal refreshes on the global 60s tick.
  For external agents, a synthesized transcript of inbox traffic
  (main → agent, rendered as `user`) merged with outbox traffic
  (agent → main, rendered as `assistant`).
* **📥 Inbox** — the live `inbox.json` plus `inbox_history.json` so the
  operator can see what was sent vs. processed.
* **⚙️ Config** — responsibilities, system_prompt, routing rules /
  capabilities, and the raw `agents.json` entry.
* **🛠️ Actions** — send-message, clear-chat, clear-session. All three
  go through the same shared helpers as `scripts/interact_with_agent.py`
  so behavior matches the CLI exactly.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

# Match `app/chat.py`'s sys.path bootstrap so we share the same
# `sys.modules["shared"]` instance the daemon uses (avoids dual-cache).
_sys_path_added = str(Path(__file__).resolve().parent.parent / "services")
if _sys_path_added not in sys.path:
    sys.path.insert(0, _sys_path_added)

from shared import (  # noqa: E402
    AGENT_CONTROL_CLEAR_CHAT,
    AGENT_CONTROL_CLEAR_SESSION,
    chat_history_path,
    read_json_file,
    set_agent_control_flag,
    write_to_inbox,
)

from app.shared import GOALS_PATH, _STATUS_COLORS, _TYPE_COLORS, _badge  # noqa: E402

# Status icons mirror app/commands_tab.py — kept local to avoid a circular
# import (commands_tab imports from app.shared, not from agents_tab).
_GOAL_STATUS_ICONS = {
    "completed": "✅",
    "failed": "❌",
    "in_progress": "🔄",
    "pending": "⏳",
}
# Order delegated goals by lifecycle: active first, terminal last.
_GOAL_STATUS_ORDER = {"in_progress": 0, "pending": 1, "completed": 2, "failed": 3}

_BASE = Path("/agent")
_AGENTS_FILE = _BASE / "memory" / "agents.json"
_EXTERNAL_DIR = _BASE / "messages" / "external"

_PORTAL_SOURCE = "portal"  # `from` field on send-message envelopes
_REPLY_TO = "messages/inbox.json"  # main inbox path string


# ─── data loaders ────────────────────────────────────────────────────────


def _load_agents() -> list[dict]:
    """Return every internal/external agent entry, sorted by name."""
    raw = read_json_file(_AGENTS_FILE, default=[])
    if not isinstance(raw, list):
        return []
    out = [
        a
        for a in raw
        if isinstance(a, dict)
        and a.get("type") in {"internal", "external"}
        and isinstance(a.get("name"), str)
        and a.get("name")
    ]
    return sorted(out, key=lambda a: a.get("name") or "")


def _load_internal_chat(name: str) -> list[dict]:
    records = read_json_file(chat_history_path(name), default=[])
    return records if isinstance(records, list) else []


def _ts(item: dict) -> str:
    """Best-effort timestamp for sorting heterogeneous envelope shapes."""
    for key in ("ts", "timestamp", "received_at", "processed_at"):
        v = item.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _synthesize_external_chat(agent: dict) -> list[dict]:
    """Build a chat-shaped transcript from an external agent's I/O files.

    Inbox = messages the *main* agent sent to the external agent → `user`.
    Outbox = messages the external agent sent back to main → `assistant`.
    Includes both the live and history files so old turns survive sweeps.
    """
    name = agent.get("name") or ""
    if not name:
        return []
    base = _EXTERNAL_DIR / name
    files: list[tuple[Path, str]] = [
        (base / "inbox.json", "user"),
        (base / "inbox_history.json", "user"),
        (base / "outbox.json", "assistant"),
        (base / "outbox_history.json", "assistant"),
    ]
    records: list[dict] = []
    for path, role in files:
        items = read_json_file(path, default=[])
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            content = str(it.get("content") or "")
            subject = str(it.get("subject") or "")
            body = subject + "\n\n" + content if subject else content
            records.append(
                {
                    "role": role,
                    "ts": _ts(it),
                    "content": body,
                    "type": it.get("type"),
                    "id": it.get("id"),
                }
            )
    records.sort(key=lambda r: r.get("ts") or "")
    return records


def _load_agent_goals(name: str) -> list[dict]:
    """Return goals from goal.json delegated to *name*.

    A goal is considered delegated to this agent iff `delegated_to.name`
    matches. Non-dict `delegated_to` values are ignored.
    """
    if not name:
        return []
    raw = read_json_file(Path(GOALS_PATH), default=[])
    if not isinstance(raw, list):
        return []
    out = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        d = g.get("delegated_to")
        if isinstance(d, dict) and d.get("name") == name:
            out.append(g)
    # Active buckets first, then newest first within each bucket. Two-stage
    # sort exploits Python's stable sort: sort by created_at desc, then by
    # status bucket — the status sort preserves the desc-by-time order.
    out.sort(key=lambda g: g.get("created_at") or "", reverse=True)
    out.sort(key=lambda g: _GOAL_STATUS_ORDER.get(g.get("status") or "pending", 99))
    return out


def _agent_inbox_path(agent: dict) -> Path | None:
    inbox = agent.get("inbox")
    return Path(inbox) if isinstance(inbox, str) and inbox else None


def _agent_inbox_history_path(agent: dict) -> Path | None:
    p = _agent_inbox_path(agent)
    if p is None:
        return None
    return p.with_name("inbox_history.json")


# ─── pure render helpers (testable) ──────────────────────────────────────


def _render_chat(history: list[dict]) -> None:
    """Render a list of {role, content, ts} records as chat bubbles.

    The daemon writes the user record to ``chat_history.json`` as soon as
    the inbound prompt is received, then appends the assistant record once
    the SDK turn finishes — so a partial transcript (user-only) is the
    expected mid-turn state and does not need a separate live buffer.
    """
    if not history:
        st.caption("(empty)")
        return
    for msg in history:
        role = msg.get("role") or "assistant"
        # Streamlit accepts only "user" / "assistant" — fall back gracefully.
        bubble_role = "user" if role == "user" else "assistant"
        with st.chat_message(bubble_role):
            ts = msg.get("ts") or ""
            if ts:
                st.caption(ts)
            content = msg.get("content") or ""
            st.markdown(str(content) if content else "_(no content)_")


def _render_inbox(agent: dict) -> None:
    inbox = _agent_inbox_path(agent)
    history = _agent_inbox_history_path(agent)
    if inbox is None:
        st.warning("This agent has no `inbox` field in agents.json.")
        return
    pending = read_json_file(inbox, default=[]) if inbox.exists() else []
    archived = (
        read_json_file(history, default=[]) if (history and history.exists()) else []
    )
    pending_list = pending if isinstance(pending, list) else []
    archived_list = archived if isinstance(archived, list) else []

    st.markdown(f"**Pending ({len(pending_list)})** · `{inbox}`")
    if not pending_list:
        st.caption("(no pending envelopes)")
    else:
        for env in pending_list:
            _render_envelope(env)

    st.divider()
    st.markdown(f"**History ({len(archived_list)})**")
    if not archived_list:
        st.caption("(no archived envelopes)")
    else:
        # Show newest first; cap at 50 to keep the page snappy.
        for env in list(reversed(archived_list))[:50]:
            _render_envelope(env)


def _render_envelope(env: dict) -> None:
    if not isinstance(env, dict):
        st.json(env)
        return
    label = " · ".join(
        [
            str(env.get("type") or "?"),
            str(env.get("from") or env.get("source") or "?"),
            _ts(env) or "?",
        ]
    )
    with st.expander(label):
        st.json(env)


def _render_goals(agent: dict, goals: list[dict] | None = None) -> None:
    """Render goals from /agent/memory/goal.json delegated to this agent.

    The main agent owns goal.json; entries with `delegated_to.name == agent`
    are work the main agent is polling — i.e. waiting on this delegate.
    """
    name = agent.get("name") or ""
    if goals is None:
        goals = _load_agent_goals(name)
    st.caption(
        "Goals from `/agent/memory/goal.json` whose `delegated_to.name` "
        f"matches `{name}`. The main agent polls these; this delegate's "
        "outbox replies (matched by `reply_to_id`) close them."
    )
    if not goals:
        st.caption("No goals delegated to this agent.")
        return
    for g in goals:
        status = g.get("status") or "pending"
        goal_id = g.get("id") or "?"
        goal_text = g.get("goal") or g.get("content") or ""
        preview = goal_text[:100] + ("..." if len(goal_text) > 100 else "")
        icon = _GOAL_STATUS_ICONS.get(status, "•")
        with st.expander(
            f"{icon} 🤝 {goal_id} — {preview}",
            expanded=(status == "in_progress"),
        ):
            s_color = _STATUS_COLORS.get(status, "#666")
            d = g.get("delegated_to") or {}
            d_type = d.get("type") or "?"
            badges = _badge(status.replace("_", " "), s_color)
            badges += " " + _badge(
                f"🤝 → {name} ({d_type})",
                _TYPE_COLORS.get("delegated", "#00BCD4"),
            )
            st.markdown(badges, unsafe_allow_html=True)
            if goal_text:
                st.markdown(goal_text)
            d_at = (g.get("delegated_at") or "")[:19].replace("T", " ")
            created = (g.get("created_at") or "")[:19].replace("T", " ")
            d_mid = str(g.get("delegated_message_id") or "")
            meta = []
            if created:
                meta.append(f"Created: {created}")
            if d_at:
                meta.append(f"Delegated at: {d_at}")
            if meta:
                st.caption(" · ".join(meta))
            if d_mid:
                st.caption(
                    f"Reply correlation: outbox messages with "
                    f"`reply_to_id == {d_mid}` close this goal."
                )
            notes = g.get("notes") or ""
            if notes:
                st.info(f"**Notes:** {notes}")


def _render_config(agent: dict) -> None:
    name = agent.get("name") or "?"
    atype = agent.get("type") or "?"
    status = agent.get("status") or "?"
    model = agent.get("model")
    st.markdown(
        " · ".join(
            [
                f"**Type:** `{atype}`",
                f"**Status:** `{status}`",
                f"**Model:** `{model}`" if model else "**Model:** _(default)_",
            ]
        )
    )
    resp = (agent.get("responsibilities") or "").strip()
    if resp:
        st.markdown("**Responsibilities**")
        st.markdown(resp)
    sp = (agent.get("system_prompt") or "").strip()
    if sp:
        st.markdown("**System prompt** (appended to shared prompt)")
        st.code(sp, language="markdown")
    rules = agent.get("outbox_routing_rules")
    if isinstance(rules, list) and rules:
        st.markdown("**Outbox routing rules**")
        st.json(rules)
    caps = agent.get("capabilities")
    if isinstance(caps, list) and caps:
        st.markdown("**Capabilities**")
        st.json(caps)
    with st.expander(f"Raw agents.json entry for `{name}`"):
        st.json(agent)


def _render_actions(agent: dict) -> None:
    name = agent.get("name") or ""
    atype = agent.get("type")

    # Mirror the "Queue Command For Next Cycle" form in
    # ``app/commands_tab.py`` — both forms write an envelope to an
    # ``inbox.json`` (main agent vs. internal/external agent) so they
    # should look and behave identically.
    st.markdown("### Send a message")
    with st.form(f"send_{name}", clear_on_submit=True):
        cmd_type = st.selectbox("Type", ["goal", "message"], key=f"type_{name}")
        subject = st.text_input(
            "Subject (optional)",
            placeholder="Short summary",
            key=f"subject_{name}",
        )
        content = st.text_area(
            "Content",
            placeholder="Enter your command or goal here...",
            key=f"content_{name}",
        )
        priority_options = ["P5", "P4", "P3", "P2", "P1"]
        priority_label = st.select_slider(
            "Priority",
            options=priority_options,
            value="P3",
            help="P1 = highest priority (right), P5 = lowest (left)",
            key=f"priority_{name}",
        )
        priority = {"P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5}[priority_label]
        submitted = st.form_submit_button("Send")
    if submitted:
        _handle_send_message(
            agent,
            cmd_type=cmd_type,
            subject=subject,
            content=content,
            priority=priority,
        )

    st.divider()
    st.markdown("### Operator actions")
    if atype == "internal":
        col1, col2 = st.columns(2)
        if col1.button("🧹 Clear chat", key=f"clear_chat_{name}"):
            _handle_clear_flag(name, AGENT_CONTROL_CLEAR_CHAT, "chat")
        if col2.button("🔄 Clear session", key=f"clear_session_{name}"):
            _handle_clear_flag(name, AGENT_CONTROL_CLEAR_SESSION, "session")
    else:
        st.caption(
            "clear_chat / clear_session are internal-agent only — external "
            "agents do not run an SDK session inside this container."
        )


def _handle_send_message(
    agent: dict,
    *,
    content: str,
    subject: str = "",
    cmd_type: str = "message",
    priority: int = 3,
) -> None:
    content = (content or "").strip()
    subject = (subject or "").strip()
    if not content:
        st.warning("Content cannot be empty.")
        return
    target = _agent_inbox_path(agent)
    if target is None:
        st.error("Agent has no `inbox` path in agents.json — cannot deliver.")
        return
    envelope: dict = {
        "id": str(uuid.uuid4()),
        "type": cmd_type,
        "subject": subject,
        "content": content,
        "source": _PORTAL_SOURCE,
        "from": _PORTAL_SOURCE,
        "priority": priority,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reply_to": _REPLY_TO,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    if write_to_inbox([envelope], inbox_file=target, dedup=False):
        st.success(f"Queued {cmd_type} command to inbox (priority {priority}).")
    else:
        st.error("write_to_inbox failed — check the daemon log.")


def _handle_clear_flag(name: str, flag: str, label: str) -> None:
    ok, matched = set_agent_control_flag([name], flag, agents_file=_AGENTS_FILE)
    if not ok:
        st.error("Failed to update agents.json (lock held / write error).")
        return
    if name in matched:
        st.success(f"Queued clear-{label} for {name!r}; daemon applies within ~10 s.")
    else:
        st.warning(f"{name!r} not found in agents.json — has it just been removed?")


# ─── top-level render ────────────────────────────────────────────────────


def render() -> None:
    st.subheader("Agents")
    agents = _load_agents()
    if not agents:
        st.info(
            "No internal or external agents are registered. Use "
            "`scripts/register_internal_agent.py` or "
            "`scripts/register_external_agent.py` to add one."
        )
        return

    labels = [
        f"{'🤖' if a.get('type') == 'internal' else '🛰️'} {a.get('name')}  "
        f"({a.get('type')}, {a.get('status') or '?'})"
        for a in agents
    ]
    idx = st.selectbox(
        "Agent",
        options=list(range(len(agents))),
        format_func=lambda i: labels[i],
        key="agents_tab_selector",
    )
    agent = agents[idx]

    # The chat tab refreshes on the global 60s portal tick. The daemon
    # writes the inbound user record to chat_history.json as soon as the
    # message is received and appends the assistant record on completion,
    # so each refresh shows the latest available state without any
    # streaming buffer.
    name = agent.get("name") or ""

    delegated_goals = _load_agent_goals(name)
    goal_label = f"🎯 Goals ({len(delegated_goals)})" if delegated_goals else "🎯 Goals"
    chat_tab, inbox_tab, goals_tab, config_tab, actions_tab = st.tabs(
        ["💬 Chat", "📥 Inbox", goal_label, "⚙️ Config", "🛠️ Actions"]
    )
    with chat_tab:
        if agent.get("type") == "internal":
            _render_chat(_load_internal_chat(name))
        else:
            st.caption(
                "External agents have no local chat_history; this is a "
                "synthesized transcript merging inbox (main → agent) and "
                "outbox (agent → main)."
            )
            _render_chat(_synthesize_external_chat(agent))
    with inbox_tab:
        _render_inbox(agent)
    with goals_tab:
        _render_goals(agent, goals=delegated_goals)
    with config_tab:
        _render_config(agent)
    with actions_tab:
        _render_actions(agent)

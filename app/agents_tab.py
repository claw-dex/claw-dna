"""Tab: Agents — per-agent chat history, inbox, config, and operator actions.

Surfaces every entry in `/agent/memory/agents.json` (internal + external)
and lets the operator inspect or interact with one agent at a time:

* **💬 Chat** — for internal agents, the daemon's `chat_history.json`. For
  external agents, a synthesized transcript of inbox traffic (main → agent,
  rendered as `user`) merged with outbox traffic (agent → main, rendered
  as `assistant`).
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from streamlit_autorefresh import st_autorefresh

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
    streaming_path,
    write_to_inbox,
)

# A streaming.json older than this is treated as stale (daemon likely died
# mid-turn before it could clean up). We still render whatever partial text
# is in the file but stop forcing the 10s refresh cadence on its account.
_STREAM_STALE_AFTER = timedelta(seconds=900)

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


def _load_streaming(name: str) -> dict | None:
    """Return the daemon's in-flight streaming buffer, or ``None`` if absent."""
    payload = read_json_file(streaming_path(name), default=None)
    if not isinstance(payload, dict):
        return None
    return payload


def _is_stream_active(payload: dict | None) -> bool:
    """True iff a streaming buffer exists and is fresher than the stale cap.

    A missing or unparseable ``started_at`` is treated as inactive — a corrupt
    buffer must not pin the UI at the 10 s refresh cadence. The daemon
    (``_flush_streaming``) always writes timestamps via ``datetime.isoformat``
    (so ``+00:00``, never ``Z``) — keep that invariant or this parse breaks.
    """
    if not payload:
        return False
    started_at = payload.get("started_at")
    if not isinstance(started_at, str):
        return False
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started < _STREAM_STALE_AFTER


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


def _agent_inbox_path(agent: dict) -> Path | None:
    inbox = agent.get("inbox")
    return Path(inbox) if isinstance(inbox, str) and inbox else None


def _agent_inbox_history_path(agent: dict) -> Path | None:
    p = _agent_inbox_path(agent)
    if p is None:
        return None
    return p.with_name("inbox_history.json")


# ─── pure render helpers (testable) ──────────────────────────────────────


def _render_chat(history: list[dict], streaming: dict | None = None) -> None:
    """Render a list of {role, content, ts} records as chat bubbles.

    If *streaming* is provided, append a live "Streaming..." assistant bubble
    after the history showing the daemon's in-flight text/tool_use/thinking
    events (mirrors `app/chat.py`'s session-state shape).
    """
    if not history and not streaming:
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
    if streaming:
        partial_text = str(streaming.get("text") or "")
        events = streaming.get("events") or []
        with st.chat_message("assistant"):
            started_at = streaming.get("started_at") or ""
            if started_at:
                st.caption(f"streaming since {started_at}")
            st.markdown(partial_text or "Processing...")
            if isinstance(events, list):
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    if ev.get("type") == "tool_use":
                        st.caption(f"Used tool: {ev.get('name') or '?'}")
                    elif ev.get("type") == "thinking":
                        with st.expander("Thinking..."):
                            st.markdown(str(ev.get("text") or ""))
            st.caption("Streaming..." if partial_text else "Thinking...")


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

    st.markdown("### Send a message")
    with st.form(f"send_{name}", clear_on_submit=True):
        subject = st.text_input("Subject (optional)", key=f"subject_{name}")
        content = st.text_area("Content", key=f"content_{name}", height=120)
        submitted = st.form_submit_button("Send")
    if submitted:
        _handle_send_message(agent, subject=subject, content=content)

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


def _handle_send_message(agent: dict, *, subject: str, content: str) -> None:
    name = agent.get("name") or ""
    target = _agent_inbox_path(agent)
    content = (content or "").strip()
    if not content:
        st.warning("Message content must not be empty.")
        return
    if target is None:
        st.error("Agent has no `inbox` path in agents.json — cannot deliver.")
        return
    envelope: dict = {
        "id": str(uuid.uuid4()),
        "type": "message",
        "content": content,
        "source": _PORTAL_SOURCE,
        "from": _PORTAL_SOURCE,
        "priority": 3,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reply_to": _REPLY_TO,
    }
    if subject and subject.strip():
        envelope["subject"] = subject.strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    if write_to_inbox([envelope], inbox_file=target, dedup=False):
        st.success(f"Sent message id={envelope['id']} to `{target}`.")
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

    # While the *currently selected* internal agent is streaming, pin the
    # refresh cadence at 10s so the live bubble updates promptly. When idle,
    # we fall back to the global 60s tick (no extra registration needed).
    name = agent.get("name") or ""
    streaming_payload: dict | None = None
    if agent.get("type") == "internal" and name:
        streaming_payload = _load_streaming(name)
        if _is_stream_active(streaming_payload):
            st_autorefresh(interval=10_000, key=f"agents_tab_stream_{name}")

    chat_tab, inbox_tab, config_tab, actions_tab = st.tabs(
        ["💬 Chat", "📥 Inbox", "⚙️ Config", "🛠️ Actions"]
    )
    with chat_tab:
        if agent.get("type") == "internal":
            _render_chat(_load_internal_chat(name), streaming=streaming_payload)
        else:
            st.caption(
                "External agents have no local chat_history; this is a "
                "synthesized transcript merging inbox (main → agent) and "
                "outbox (agent → main)."
            )
            _render_chat(_synthesize_external_chat(agent))
    with inbox_tab:
        _render_inbox(agent)
    with config_tab:
        _render_config(agent)
    with actions_tab:
        _render_actions(agent)

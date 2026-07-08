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
* **🏥 Health** — per-agent error history sourced from `server_errors.json`.
  Shows total error count, errors in the last 24h, last error timestamp, and
  an expandable log of every entry.  Service-level turn timeouts (logged by
  the internal_agent_chat daemon) appear here so recurring SDK failures are
  visible without grepping log files.  Tab label shows `(N)` when errors
  exist for the selected agent.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
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
_SERVER_ERRORS_FILE = _BASE / "memory" / "server_errors.json"

_PORTAL_SOURCE = "portal"  # `from` field on send-message envelopes
_REPLY_TO = "messages/inbox.json"  # main inbox path string

# Threshold constants for agent health panel
_HEALTH_RECENT_HOURS = 24  # errors within this window are "recent"
_HEALTH_WARNING_COUNT = 3  # ≥ this many errors in 24h triggers warning

# ─── mtime-based loader caches ───────────────────────────────────────────
# Each dict maps a file path string to (result, mtime) so the 10-second
# _render_chat_fragment refresh loop reads disk only when a file changes.

_AGENTS_CACHE: dict = {}  # {str(path): (result, mtime)}
_CHAT_CACHE: dict = {}  # {str(path): (result, mtime)}

# Goal and error lists are cached by (path, mtime) so repeated calls within a
# single portal render (render() + _render_health() both call
# _load_agent_errors()) share one disk read instead of two.
# Keyed by str(path) — same pattern as _AGENTS_CACHE — so test fixtures
# that redirect the module-level path constants to a temp sandbox each get
# their own cache entry (prevents mtimes from different paths colliding).
_GOALS_CACHE: dict = {}  # {str(path): (all_goals_list, mtime)}
_ERRORS_CACHE: dict = {}  # {str(path): (all_errors_list, mtime)}
# _synthesize_external_chat reads 4 JSON files per agent on every 10s fragment
# tick.  Cache key: agent name → (result, tuple[4 mtimes]).  A cache miss
# only fires when at least one of the four files has changed since last read.
_EXTERNAL_CHAT_CACHE: dict = {}  # {name: (result, tuple[mtime, ...])}
# must be ≥ 0 (exact mtime equality — standard pattern across all loader caches)
_EXTERNAL_CHAT_CACHE_MIN_MTIME: float = 0.0


# ─── data loaders ────────────────────────────────────────────────────────


def _file_mtime(path: Path) -> float:
    """Return path's mtime, or 0.0 if the file doesn't exist / can't be stat'd."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _load_agents() -> list[dict]:
    """Return every internal/external agent entry, sorted by name.

    Mtime-cached: avoids re-reading agents.json on every 10s fragment tick.
    Cache key includes the path string so tests can safely redirect _AGENTS_FILE
    to a temp directory without hitting stale results from a previous path.
    """
    path = _AGENTS_FILE
    path_str = str(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    cached = _AGENTS_CACHE.get(path_str)
    if cached is not None:
        result, c_mtime = cached
        if c_mtime == mtime:
            return result
    raw = read_json_file(path, default=[])
    if not isinstance(raw, list):
        result = []
    else:
        out = [
            a
            for a in raw
            if isinstance(a, dict)
            and a.get("type") in {"internal", "external"}
            and isinstance(a.get("name"), str)
            and a.get("name")
        ]
        result = sorted(out, key=lambda a: a.get("name") or "")
    _AGENTS_CACHE[path_str] = (result, mtime)
    return result


def _load_internal_chat(name: str) -> list[dict]:
    """Return chat history for *name*, mtime-cached.

    chat_history.json is read every 10s by _render_chat_fragment — mtime
    caching eliminates disk reads between daemon writes (i.e. when no new
    message has arrived or been processed since the last tick).
    """
    path = chat_history_path(name)
    path_str = str(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    cached = _CHAT_CACHE.get(path_str)
    if cached is not None:
        result, c_mtime = cached
        if c_mtime == mtime:
            return result
    records = read_json_file(path, default=[])
    result = records if isinstance(records, list) else []
    _CHAT_CACHE[path_str] = (result, mtime)
    return result


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

    Mtime-cached on the 4 source files: the 10-second _render_chat_fragment
    tick re-uses the cached result as long as none of the 4 files has changed.
    Cache key: agent name → (result, tuple[4 mtimes]).
    """
    name = agent.get("name") or ""
    if not name:
        return []
    base = _EXTERNAL_DIR / name
    file_specs: list[tuple[Path, str]] = [
        (base / "inbox.json", "user"),
        (base / "inbox_history.json", "user"),
        (base / "outbox.json", "assistant"),
        (base / "outbox_history.json", "assistant"),
    ]
    # Compute current mtimes for all 4 files (0.0 if missing)
    current_mtimes = tuple(_file_mtime(path) for path, _ in file_specs)
    # Cache hit: return stored result when all 4 mtimes are unchanged
    cached = _EXTERNAL_CHAT_CACHE.get(name)
    if cached is not None:
        result, cached_mtimes = cached
        if cached_mtimes == current_mtimes:
            return result
    # Cache miss: read all 4 files and compute transcript
    records: list[dict] = []
    for path, role in file_specs:
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
    _EXTERNAL_CHAT_CACHE[name] = (records, current_mtimes)
    return records


def _load_agent_goals(name: str) -> list[dict]:
    """Return goals from goal.json delegated to *name*.

    A goal is considered delegated to this agent iff `delegated_to.name`
    matches. Non-dict `delegated_to` values are ignored.

    Uses mtime-based caching: the full goal list is cached on goal.json's mtime
    so repeated calls within one portal render (render() calls this for label
    computation) re-use the cached list instead of re-reading from disk.
    Previously read_json_file(GOALS_PATH) ran on every invocation regardless
    of whether goal.json had changed.
    """
    if not name:
        return []

    path = Path(GOALS_PATH)
    path_str = str(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0

    cached = _GOALS_CACHE.get(path_str)
    if cached is not None:
        all_goals, c_mtime = cached
        if c_mtime == mtime:
            raw = all_goals
        else:
            raw = None
    else:
        raw = None

    if raw is None:
        raw = read_json_file(path, default=[])
        if not isinstance(raw, list):
            raw = []
        _GOALS_CACHE[path_str] = (raw, mtime)

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


def _load_agent_errors(name: str) -> list[dict]:
    """Return server_errors.json entries associated with *name*.

    Matches entries where the ``context`` field starts with ``<name>:``
    (service-level errors logged by the internal_agent_chat daemon) or
    where ``tab`` == ``name`` (direct tab rendering errors).  Returns
    entries newest-first.

    Uses mtime-based caching: the full error list is cached on
    server_errors.json's mtime.  render() calls _load_agent_errors() once
    for the tab label, then _render_health() calls it again — without
    caching that was two read_json_file calls per render.  With caching the
    second call is a single dict.get() + one os.stat(), saving a disk read.
    """
    if not name:
        return []

    path = _SERVER_ERRORS_FILE
    path_str = str(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0

    cached = _ERRORS_CACHE.get(path_str)
    if cached is not None:
        all_errors, c_mtime = cached
        if c_mtime == mtime:
            raw = all_errors
        else:
            raw = None
    else:
        raw = None

    if raw is None:
        raw = read_json_file(path, default=[])
        if not isinstance(raw, list):
            raw = []
        _ERRORS_CACHE[path_str] = (raw, mtime)

    prefix = f"{name}:"
    out = [
        e
        for e in raw
        if isinstance(e, dict)
        and (str(e.get("context", "")).startswith(prefix) or e.get("tab") == name)
    ]
    out.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
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


_CHAT_TAIL_LIMIT = 10


def _caddy_url(abs_path: str | Path) -> str:
    """Map an absolute path under /agent to its Caddy file-browser URL.

    Paths outside ``/agent/`` are returned unchanged (passthrough). Path
    segments are percent-encoded so agent names containing parens, spaces,
    or other markdown-sensitive characters cannot break the rendered link.
    """
    p = str(abs_path)
    if p.startswith("/agent/"):
        return "/_" + urllib.parse.quote(p, safe="/")
    return p


@st.fragment(run_every="10s")
def _render_chat_fragment(name: str) -> None:
    """Refresh the chat transcript every 10s without rerunning the whole page.

    Takes the agent ``name`` (hashable) rather than the agent dict — Streamlit
    uses fragment args for instance identity, so passing a mutable dict would
    invalidate the fragment whenever any field on the agent record changes.
    The agent record is re-looked-up from agents.json on each tick.
    """
    agent = next((a for a in _load_agents() if a.get("name") == name), None)
    if agent is None:
        st.caption(f"Agent {name!r} no longer registered.")
        return
    if agent.get("type") == "internal":
        _render_chat(
            _load_internal_chat(name),
            source_path=str(chat_history_path(name)),
        )
    else:
        st.caption(
            "External agents have no local chat_history; this is a "
            "synthesized transcript merging inbox (main → agent) and "
            "outbox (agent → main)."
        )
        _render_chat(_synthesize_external_chat(agent))


def _render_chat(history: list[dict], source_path: str | None = None) -> None:
    """Render a list of {role, content, ts} records as chat bubbles.

    Only the last ``_CHAT_TAIL_LIMIT`` records are rendered. When the history
    is longer, a header banner shows how many older messages are hidden and
    links to the full file via the Caddy file browser (if ``source_path``
    is supplied).

    The daemon writes the user record to ``chat_history.json`` as soon as
    the inbound prompt is received, then appends the assistant record once
    the SDK turn finishes — so a partial transcript (user-only) is the
    expected mid-turn state and does not need a separate live buffer.
    """
    if not history:
        st.caption("(empty)")
        return
    older = max(0, len(history) - _CHAT_TAIL_LIMIT)
    if older > 0:
        if source_path:
            url = _caddy_url(source_path)
            st.markdown(
                f"📜 _Showing last {_CHAT_TAIL_LIMIT} of {len(history)} messages — "
                f"[{older} older in full history]({url})_"
            )
        else:
            st.markdown(
                f"📜 _Showing last {_CHAT_TAIL_LIMIT} of {len(history)} messages "
                f"({older} older hidden)_"
            )
    for msg in history[-_CHAT_TAIL_LIMIT:]:
        role = msg.get("role") or "assistant"
        # Streamlit accepts only "user" / "assistant" — fall back gracefully.
        bubble_role = "user" if role == "user" else "assistant"
        with st.chat_message(bubble_role):
            ts = msg.get("ts") or ""
            if ts:
                st.caption(ts)
            content = msg.get("content") or ""
            st.markdown(str(content) if content else "_(no content)_")
            thinking = msg.get("thinking")
            if thinking:
                # One turn may emit several ThinkingBlocks; the daemon
                # already merges them into a single string before
                # persisting (see `_run_turn_for_group` /
                # `_append_assistant_record` in internal_agent_chat.py),
                # so this is always one collapsed row per assistant turn,
                # not one per block.
                with st.expander("Thinking..."):
                    st.markdown(str(thinking))


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


def _from_label(env: dict) -> str | None:
    """Human-readable sender for an envelope's structured (or legacy) `from`."""
    frm = env.get("from")
    if isinstance(frm, dict):
        return (
            frm.get("handle")
            or frm.get("source")
            or frm.get("transport")
            or frm.get("raw")
        )
    return str(frm) if frm else None


def _render_envelope(env: dict) -> None:
    if not isinstance(env, dict):
        st.json(env)
        return
    label = " · ".join(
        [
            str(env.get("type") or "?"),
            str(_from_label(env) or env.get("source") or "?"),
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

    # Mirror the form in
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
        # source="portal" (origin). No transport — the portal writes directly to
        # the inbox, so source already says how it arrived (no duplication).
        # Inlined to avoid a cross-root import from app/ into services/.
        "from": {"source": _PORTAL_SOURCE, "role": "owner"},
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


def _render_health(agent: dict) -> None:
    """Render the Health tab for a selected agent.

    Shows per-agent errors from server_errors.json — including service-level
    turn timeouts logged by the internal_agent_chat daemon — so the operator
    can spot recurring issues at a glance without grepping log files.
    """
    name = agent.get("name") or ""
    errors = _load_agent_errors(name)

    now = datetime.now(timezone.utc)
    recent = [
        e
        for e in errors
        if e.get("timestamp")
        and (
            now - datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        ).total_seconds()
        / 3600
        < _HEALTH_RECENT_HOURS
    ]

    # ── Summary metrics ──────────────────────────────────────────────
    c1, c2, c3 = st.columns(3)
    with c1:
        total = len(errors)
        st.metric("Total errors (all time)", total)
    with c2:
        recent_count = len(recent)
        delta_color = "normal" if recent_count == 0 else "inverse"
        st.metric(
            f"Errors (last {_HEALTH_RECENT_HOURS}h)",
            recent_count,
            delta=("⚠ active" if recent_count >= _HEALTH_WARNING_COUNT else None),
            delta_color=delta_color,
        )
    with c3:
        if errors:
            last_ts = errors[0].get("timestamp", "")
            if last_ts:
                try:
                    last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                    delta_secs = (now - last_dt).total_seconds()
                    if delta_secs < 3600:
                        ago = f"{int(delta_secs // 60)}m ago"
                    elif delta_secs < 86400:
                        ago = f"{int(delta_secs // 3600)}h ago"
                    else:
                        ago = f"{int(delta_secs // 86400)}d ago"
                    st.metric("Last error", ago)
                except Exception:
                    st.metric("Last error", last_ts[:10])
        else:
            st.metric("Last error", "—")

    # ── Health status banner ────────────────────────────────────────
    if not errors:
        st.success(f"No errors logged for **{name}**.")
        return

    if recent_count >= _HEALTH_WARNING_COUNT:
        st.warning(
            f"{recent_count} error(s) in the last {_HEALTH_RECENT_HOURS}h — "
            "consider restarting the agent or checking the service log."
        )
    elif recent_count > 0:
        st.info(
            f"{recent_count} error(s) in the last {_HEALTH_RECENT_HOURS}h "
            "(below warning threshold)."
        )
    else:
        st.success(f"No recent errors for **{name}** (last {_HEALTH_RECENT_HOURS}h).")

    # ── Error log table ─────────────────────────────────────────────
    st.subheader("Error log")
    st.caption(
        f"Showing {len(errors)} entries from server_errors.json filtered for `{name}`. "
        "Entries expire after the log reaches 20 items."
    )

    for err in errors:
        ts = err.get("timestamp", "")
        error_msg = err.get("error", "unknown")
        error_type = err.get("error_type", "")
        context = err.get("context", "")
        source_type = err.get("source_type", "")

        # Compute age
        age_str = ""
        if ts:
            try:
                err_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                delta_secs = (now - err_dt).total_seconds()
                if delta_secs < 3600:
                    age_str = f"{int(delta_secs // 60)}m ago"
                elif delta_secs < 86400:
                    age_str = f"{int(delta_secs // 3600)}h ago"
                else:
                    age_str = f"{int(delta_secs // 86400)}d ago"
            except Exception:
                age_str = ts[:10]

        is_recent = err in recent
        icon = "🔴" if is_recent else "⚪"
        label = f"{icon} `{ts[:16]}` ({age_str}) — **{error_msg}**" + (
            f" [{source_type}]" if source_type else ""
        )
        with st.expander(label, expanded=is_recent):
            if error_type:
                st.caption(f"Type: `{error_type}`")
            if context:
                st.caption(f"Context: `{context}`")


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
    agent_errors = _load_agent_errors(name)
    health_label = f"🏥 Health ({len(agent_errors)})" if agent_errors else "🏥 Health"

    tab_labels = [
        "💬 Chat",
        "📥 Inbox",
        goal_label,
        "⚙️ Config",
        "🛠️ Actions",
        health_label,
    ]

    tabs = st.tabs(tab_labels)
    chat_tab, inbox_tab, goals_tab, config_tab, actions_tab, health_tab = tabs
    with chat_tab:
        # Chat transcript polls on a 10s cadence via st.fragment, independent
        # of the global 60s portal tick.
        _render_chat_fragment(name)
    with inbox_tab:
        _render_inbox(agent)
    with goals_tab:
        _render_goals(agent, goals=delegated_goals)
    with config_tab:
        _render_config(agent)
    with actions_tab:
        _render_actions(agent)
    with health_tab:
        _render_health(agent)

"""
Tab: Command Center — goal/message form, scheduled tasks, goals, inbox/outbox, command history.

Enum Reference: See prompts/enum.md → Message Type for valid command types, Goal Status for status values.
"""

from datetime import datetime, timezone

import streamlit as st

from app.shared import _badge, _STATUS_COLORS, _TYPE_COLORS

# ── Status / type icons (tab-specific, not in shared) ────────
# See prompts/enum.md for complete enum definitions
_STATUS_ICONS = {
    "completed": "✅",
    "failed": "❌",
    "in_progress": "🔄",
    "pending": "⏳",
}
_TYPE_ICONS = {
    # Inbox types
    "goal": "🎯",
    "message": "💬",
    "bash": "⚡",
    # Outbox types
    "response": "📤",
    "needs_human": "🆘",
    "goal_complete": "✅",
    "goal_failed": "❌",
}


def render():
    from app.data import (
        load_goals,
        load_inbox,
        load_outbox,
        load_outbox_history,
        load_history,
        queue_to_inbox,
        update_goal_status,
        delete_inbox_item,
        clear_outbox,
    )

    # ── Command form ──────────────────────────────────────────
    st.subheader("Queue Command For Next Cycle")
    with st.form("command_form", clear_on_submit=True):
        cmd_type = st.selectbox("Type", ["goal", "message"])
        content = st.text_area(
            "Content", placeholder="Enter your command or goal here..."
        )
        priority = st.slider(
            "Priority", 1, 5, 3, help="1 = highest priority, 5 = lowest"
        )
        submitted = st.form_submit_button("Send")

    if submitted:
        if not content or not content.strip():
            st.error("Content cannot be empty.")
        else:
            ts = datetime.now(timezone.utc).isoformat()
            queue_to_inbox(content.strip(), cmd_type, ts, priority=priority)
            st.success(f"Queued {cmd_type} command to inbox (priority {priority}).")

    st.divider()

    # ── Cycle command history ─────────────────────────────────
    col_title, col_limit = st.columns([3, 1])
    with col_title:
        st.subheader("Cycle Command History")
    with col_limit:
        show_n = st.selectbox(
            "Show",
            [1, 5, 25, 100],
            index=0,
            key="cmd_hist_limit",
            label_visibility="collapsed",
        )
    cmd_history = load_history() or []
    outbox_hist = load_outbox_history() or []

    # Merge user commands and agent responses into a unified timeline
    events = []
    for cmd in cmd_history:
        ts = cmd.get("timestamp", "")
        events.append(
            {
                "ts": ts,
                "role": "user",
                "type": cmd.get("type", "?"),
                "content": cmd.get("content", ""),
            }
        )
    for msg in outbox_hist:
        ts = msg.get("timestamp", "")
        subject = msg.get("subject", "")
        content = msg.get("content", "")
        events.append(
            {
                "ts": ts,
                "role": "agent",
                "type": msg.get("type", "response"),
                "subject": subject,
                "content": content,
            }
        )
    events.sort(key=lambda e: e.get("ts", ""))

    if not events:
        st.caption("No command history yet.")
    else:
        # Show most recent N, most recent first
        for ev in reversed(events[-show_n:]):
            ts_str = str(ev.get("ts", ""))[:19].replace("T", " ")
            if ev["role"] == "user":
                label = f"**You** [{ev['type']}]  ·  {ts_str}"
                with st.chat_message("user"):
                    st.caption(label)
                    st.write(
                        ev["content"][:500]
                        + ("..." if len(ev["content"]) > 500 else "")
                    )
            else:
                subj = ev.get("subject", "")
                is_needs_human = ev.get("type") == "needs_human"
                label = f"**Agent**  ·  {ts_str}" + (f"  ·  _{subj}_" if subj else "")
                with st.chat_message("assistant"):
                    st.caption(label)
                    body = ev.get("content", "")
                    if is_needs_human:
                        st.warning(body if len(body) <= 800 else body[:800] + "...")
                        if len(body) > 800:
                            with st.expander("Show full message"):
                                st.warning(body)
                    elif len(body) > 800:
                        with st.expander("Show full response"):
                            st.markdown(body)
                        st.markdown(body[:800] + "...")
                    else:
                        st.markdown(body)

    st.divider()

    # ── Goals / Inbox / Outbox ─────────────────────────────────
    goals = load_goals() or []
    inbox = load_inbox() or []
    outbox = load_outbox() or []

    active_goals = [g for g in goals if g.get("status") in ("in_progress", "pending")]

    # Summary metrics strip
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric(
            "Active Goals",
            len(active_goals),
            help=f"{len(goals)} total goals" if goals else None,
        )
    with m2:
        st.metric("Inbox", len(inbox))
    with m3:
        st.metric("Outbox", len(outbox))

    # Tabbed layout
    tab_goals, tab_inbox, tab_outbox = st.tabs(
        [
            f"Goals ({len(goals)})",
            f"Inbox ({len(inbox)})",
            f"Outbox ({len(outbox)})",
        ]
    )

    with tab_goals:
        if not goals:
            st.caption("No goals yet.")
        else:
            for i, g in enumerate(goals):
                status = g.get("status", "pending")
                goal_id = g.get("id", f"goal-{i+1}")
                goal_text = g.get("goal") or g.get("content") or ""
                preview = goal_text[:100] + ("..." if len(goal_text) > 100 else "")
                icon = _STATUS_ICONS.get(status, "•")
                created = (g.get("created_at") or "")[:10]

                with st.expander(
                    f"{icon} {goal_id} — {preview}", expanded=(status == "in_progress")
                ):
                    # Status + source badges
                    s_color = _STATUS_COLORS.get(status, "#666")
                    source = g.get("source", "")
                    badges = _badge(status.replace("_", " ").replace("-", " "), s_color)
                    if source:
                        badges += " " + _badge(f"source: {source}", "#555")
                    st.markdown(badges, unsafe_allow_html=True)

                    st.markdown(goal_text)

                    if created:
                        st.caption(f"Created: {created}")
                    notes = g.get("notes", "")
                    if notes:
                        st.info(f"**Notes:** {notes}")

                    # Status change control
                    status_options = ["pending", "in_progress", "completed", "failed"]
                    # Normalize legacy "in-progress" to "in_progress"
                    status_normalized = (
                        status.replace("-", "_") if status else "pending"
                    )
                    current_idx = (
                        status_options.index(status_normalized)
                        if status_normalized in status_options
                        else 0
                    )
                    new_status = st.selectbox(
                        "Change status",
                        status_options,
                        index=current_idx,
                        key=f"goal_status_{i}",
                    )
                    if new_status != status_normalized:
                        update_goal_status(i, new_status)
                        st.rerun()

    with tab_inbox:
        if not inbox:
            st.caption("Empty inbox.")
        else:
            for i, item in enumerate(inbox):
                cmd_t = item.get("type", "?")
                content = item.get("content") or ""
                preview = content[:100] + ("..." if len(content) > 100 else "")
                ts = str(item.get("timestamp", ""))[:19].replace("T", " ")
                t_color = _TYPE_COLORS.get(cmd_t, "#666")
                t_icon = _TYPE_ICONS.get(cmd_t, "•")

                with st.expander(f"{t_icon} [{cmd_t}] {preview}"):
                    st.markdown(_badge(cmd_t, t_color), unsafe_allow_html=True)
                    st.markdown(content)
                    st.caption(f"Queued: {ts}")
                    if st.button("Delete", key=f"del_inbox_{i}"):
                        delete_inbox_item(i)
                        st.rerun()

    with tab_outbox:
        if not outbox:
            st.caption("Empty outbox.")
        else:
            if st.button(
                "Archive & Clear All",
                key="clear_outbox",
                help="Archives messages to history before clearing",
            ):
                clear_outbox()
                st.rerun()
            for i, item in enumerate(outbox):
                item_type = item.get("type", "?")
                subject = item.get("subject", "")
                content = item.get("content") or ""
                ts = str(item.get("timestamp", ""))[:19].replace("T", " ")
                is_needs_human = item_type == "needs_human"

                label = subject or content[:100] or f"[{item_type}]"
                if len(label) > 100:
                    label = label[:100] + "..."
                prefix = "🔔" if is_needs_human else "📤"

                with st.expander(f"{prefix} {label}", expanded=is_needs_human):
                    if is_needs_human:
                        st.warning(f"**{subject or 'Action required'}**\n\n{content}")
                    else:
                        if subject:
                            st.markdown(f"**{subject}**")
                        st.markdown(content)
                    st.caption(f"Sent: {ts}")

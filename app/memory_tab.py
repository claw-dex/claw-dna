"""Tab: Memory — consolidated Journal + Logs + Goals + Memory Files."""

import io
import re

import streamlit as st

from app.shared import (
    _STATUS_COLORS,
    _STATUS_MD_COLORS,
    _badge,
    message_source,
)


def _format_size(num_bytes) -> str:
    if num_bytes is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return (
                f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
            )
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _show_image(path: str, caption: str) -> None:
    """Render an image from a local file path using PIL bytes (most robust approach)."""
    try:
        from PIL import Image

        with open(path, "rb") as f:
            data = f.read()
        img = Image.open(io.BytesIO(data))
        st.image(img, caption=caption)
    except Exception as exc:
        st.error(f"Could not display image {caption}: {exc}")


def render():
    from app.data.metrics import load_memory_overview
    from app.data import (
        load_journal,
        load_goals,
        load_logs,
        load_log_detail,
        load_inbox_history,
        load_cycles,
        load_memory_files,
        read_memory_file,
    )

    # ── Memory Overview (top section) ─────────────────────────
    # Pre-computed by scripts/metrics_db.py — these are counts over journal,
    # cycles, and the message histories, plus a walk of the LanceDB store.
    # Deriving them here meant six full JSON parses per render.
    overview = load_memory_overview()
    journal_n = overview["journal_active"]
    archive_n = overview["journal_archived"]
    cycles_n = overview["cycles_active"]
    cycles_archive_n = overview["cycles_archived"]

    st.subheader("Memory Overview")
    o1, o2, o3, o4, o5 = st.columns(5)
    with o1:
        st.metric("Long-term Memory", _format_size(overview["ltm_bytes"]))
        st.caption("long_term_memory.lancedb")
    with o2:
        st.metric("Journal Entries", f"{journal_n + archive_n}")
        st.caption(f"{journal_n} active · {archive_n} archived")
    with o3:
        st.metric("Inbox History", f"{overview['inbox_history']}")
        st.caption(_format_size(overview["inbox_history_bytes"]))
    with o4:
        st.metric("Outbox History", f"{overview['outbox_history']}")
        st.caption(_format_size(overview["outbox_history_bytes"]))
    with o5:
        st.metric("Cycles", f"{cycles_n + cycles_archive_n}")
        st.caption(f"{cycles_n} active · {cycles_archive_n} archived")

    st.divider()

    # ── 4 sub-tabs ────────────────────────────────────────────
    tab_journal, tab_logs, tab_goals, tab_memfiles = st.tabs(
        ["📓 Journal", "📋 Logs", "🎯 Goals", "🗂 Memory Files"]
    )

    # ── Journal sub-tab ───────────────────────────────────────
    with tab_journal:
        PAGE_SIZE = 20
        if "journal_offset" not in st.session_state:
            st.session_state.journal_offset = 0

        offset = st.session_state.journal_offset
        result = load_journal(limit=PAGE_SIZE, offset=offset)
        entries = result.get("entries", [])
        total = result.get("total", 0)
        has_more = result.get("has_more", False)

        st.subheader(f"Journal ({total} entries)")

        if not entries:
            st.info("No journal entries yet. The agent writes here after each cycle.")
        else:
            for entry in entries:
                cycle = entry.get("cycle_number", "?")
                goal = entry.get("cycle_goal", "Unknown")
                status = entry.get("cycle_status", "completed")
                ts = str(entry.get("timestamp", ""))[:19].replace("T", " ")
                status_icon = {
                    "completed": "✅",
                    "failed": "❌",
                    "in_progress": "🔄",
                }.get(status, "⏳")
                status_md_color = _STATUS_MD_COLORS.get(status, "gray")
                status_label = (status or "pending").replace("_", " ")

                with st.expander(
                    f"{status_icon} :{status_md_color}[**{status_label}**] · "
                    f"Cycle {cycle} — {goal[:60]} `{ts}`"
                ):
                    entry_type = entry.get("cycle_type", "")
                    category = entry.get("cycle_category", "")
                    actions = entry.get("actions", [])
                    summary = entry.get("summary") or entry.get("outcome", "")
                    if entry_type:
                        st.caption(
                            f"Type: {entry_type}"
                            + (f" · Category: {category}" if category else "")
                        )
                    st.markdown(f"**Goal:** {goal}")
                    if actions:
                        st.markdown("**Actions:**")
                        for action in actions:
                            st.markdown(f"- {action}")
                    if summary:
                        st.markdown(f"**Summary:** {summary}")
                    if not actions and not summary:
                        st.caption("(No details)")

        # Pagination controls
        col_prev, col_info, col_next = st.columns([1, 2, 1])
        with col_prev:
            if offset > 0:
                if st.button("← Previous", key="mem_journal_prev"):
                    st.session_state.journal_offset = max(0, offset - PAGE_SIZE)
                    st.rerun()
        with col_info:
            page_num = offset // PAGE_SIZE + 1
            total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE if total > 0 else 1
            st.caption(f"Page {page_num} of {total_pages} ({total} total)")
        with col_next:
            if has_more:
                if st.button("Next →", key="mem_journal_next"):
                    st.session_state.journal_offset = offset + PAGE_SIZE
                    st.rerun()

    # ── Logs sub-tab ──────────────────────────────────────────
    with tab_logs:
        # Bash logs
        st.subheader("Bash Logs")
        logs = load_logs() or []

        if not logs:
            st.caption("No bash logs yet.")
        else:
            for i, log in enumerate(reversed(logs)):
                num = log.get("command_num", "?")
                cmd = (log.get("command") or "")[:60]
                status = log.get("status", "unknown")
                started = str(log.get("started_at", ""))[:19].replace("T", " ")
                duration = log.get("duration_seconds")
                dur_str = f" ({duration}s)" if duration is not None else ""

                status_badge = {
                    "completed": "✅",
                    "failed": "❌",
                    "running": "⏳",
                    "exited": "⚠️",
                }.get(status, "•")

                col1, col2 = st.columns([4, 1])
                with col1:
                    st.markdown(
                        f"{status_badge} **#{num}** `{cmd}` — {started}{dur_str}"
                    )
                detail_key = f"mem_show_log_{num}"
                with col2:
                    if st.button("Details", key=f"mem_log_detail_btn_{i}_{num}"):
                        st.session_state[detail_key] = not st.session_state.get(
                            detail_key, False
                        )

                if st.session_state.get(detail_key, False):
                    detail = load_log_detail(num)
                    if detail:
                        with st.container():
                            st.caption(
                                f"PID: {detail.get('pid')} | Exit code: {detail.get('exit_code', 'N/A')}"
                            )
                            tab_out, tab_err = st.tabs(["stdout", "stderr"])
                            with tab_out:
                                stdout = detail.get("stdout", "")
                                st.code(
                                    stdout if stdout else "(empty)", language="text"
                                )
                            with tab_err:
                                stderr = detail.get("stderr", "")
                                st.code(
                                    stderr if stderr else "(empty)", language="text"
                                )
                            if st.button("Refresh", key=f"mem_refresh_log_{num}"):
                                load_log_detail.clear()
                                st.rerun()
                    else:
                        st.warning(f"Log #{num} not found.")

        st.divider()

        # Command history — sourced from inbox_history.json (archived inbox
        # captures portal submissions plus Telegram / agent-forwarded inputs).
        st.subheader("Command History")
        history = load_inbox_history() or []

        if not history:
            st.caption("No command history yet.")
        else:
            type_icons = {"goal": "🎯", "message": "💬", "bash": "💻"}
            for cmd in reversed(history[-30:]):
                if not isinstance(cmd, dict):
                    continue
                cmd_type = cmd.get("type", "?")
                icon = type_icons.get(cmd_type, "•")
                raw = cmd.get("content") or ""
                if not isinstance(raw, str):
                    raw = str(raw)
                content = raw[:80]
                source = message_source(cmd) or cmd.get("channel") or ""
                ts = str(cmd.get("timestamp", ""))[:19].replace("T", " ")
                st.markdown(f"{icon} **[{cmd_type}]** {content}")
                st.caption(f"{source} | {ts}" if source else ts)

        st.divider()

        # Cycle timeline
        st.subheader("Cycle Timeline")
        cycles = load_cycles() or []

        if not cycles:
            st.caption("No cycles recorded yet.")
        else:
            recent = cycles[-10:]
            cols = st.columns(min(len(recent), 10))
            for i, cycle in enumerate(recent):
                col_idx = i % len(cols)
                with cols[col_idx]:
                    cycle_num = cycle.get("cycle", "?")
                    c_status = cycle.get("status", "unknown")
                    dur = cycle.get("duration_seconds")
                    cycle_type = cycle.get("type", "")
                    c_icon = {
                        "completed": "✅",
                        "failed": "❌",
                        "in_progress": "🔄",
                    }.get(c_status, "⏳")
                    st.markdown(f"{c_icon} **#{cycle_num}**")
                    if cycle_type:
                        st.caption(cycle_type[:12])
                    if dur is not None:
                        st.caption(f"{dur}s")

            with st.expander(f"All {len(cycles)} cycles"):
                rows = []
                for c in reversed(cycles):
                    rows.append(
                        {
                            "Cycle": c.get("cycle_number", ""),
                            "Type": c.get("cycle_type", ""),
                            "Status": c.get("cycle_status", ""),
                            "Duration (s)": c.get("duration_seconds"),
                            "Goal": (c.get("cycle_goal") or "")[:60],
                            "Start": str(c.get("start", ""))[:19].replace("T", " "),
                        }
                    )
                st.dataframe(rows, width="stretch")

    # ── Goals sub-tab ─────────────────────────────────────────
    with tab_goals:
        st.subheader("Goals")
        goals = load_goals() or []

        if not goals:
            st.info("No goals yet.")
        else:
            # Status filter
            all_statuses = sorted(set(g.get("status", "unknown") for g in goals))
            selected_status = st.selectbox(
                "Filter by status",
                ["all"] + all_statuses,
                key="mem_goal_status_filter",
            )
            filtered = (
                goals
                if selected_status == "all"
                else [g for g in goals if g.get("status") == selected_status]
            )

            status_icons = {
                "completed": "✅",
                "failed": "❌",
                "in_progress": "🔄",
                "pending": "⏳",
            }

            # Summary metrics
            total = len(goals)
            completed = sum(1 for g in goals if g.get("status") == "completed")
            failed = sum(1 for g in goals if g.get("status") == "failed")
            in_progress = sum(1 for g in goals if g.get("status") == "in_progress")
            pending = sum(1 for g in goals if g.get("status") == "pending")

            m1, m2, m3, m4, m5 = st.columns(5)
            with m1:
                st.metric("Total", total)
            with m2:
                st.metric("Completed", completed)
            with m3:
                st.metric("Failed", failed)
            with m4:
                st.metric("In Progress", in_progress)
            with m5:
                st.metric("Pending", pending)

            st.caption(f"Showing {len(filtered)} of {total} goals")

            for g in reversed(filtered):
                g_status = g.get("status", "unknown")
                icon = status_icons.get(g_status, "•")
                content = g.get("content") or g.get("goal") or ""
                created = (g.get("created_at") or g.get("source_timestamp") or "")[
                    :19
                ].replace("T", " ")
                with st.expander(f"{icon} {content[:80]} `{g_status}`"):
                    s_color = _STATUS_COLORS.get(g_status, "#666")
                    st.markdown(
                        _badge(g_status.replace("_", " "), s_color),
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"**Goal:** {content}")
                    st.caption(f"Created: {created}")
                    if g.get("id"):
                        st.caption(f"ID: {g['id']}")

    # ── Memory Files sub-tab ──────────────────────────────────
    with tab_memfiles:
        st.subheader("Memory Files")
        memory_files = [f for f in (load_memory_files() or []) if f.endswith(".md")]

        if not memory_files:
            st.caption("No markdown memory files found in /agent/memory/")
        else:
            selected_mem = st.selectbox(
                "Select file", memory_files, key="mem_file_select"
            )
            if selected_mem:
                content = read_memory_file(selected_mem)
                if content is None:
                    st.error(f"Could not read: {selected_mem}")
                else:
                    fixed = re.sub(r"(?<![A-Za-z0-9_/])/agent/", "/_/agent/", content)
                    st.markdown(fixed)
                    st.caption(f"Size: {len(content)} chars")

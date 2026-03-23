"""Tab: Memory — consolidated Journal + Logs + Goals + Memory Files with top-level search."""

import io

import streamlit as st


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
    from app.data import (
        load_journal, load_goals, load_logs, load_log_detail,
        load_history, load_cycles, load_memory_files, read_memory_file,
        search as do_search,
    )

    # ── Top-level search bar ──────────────────────────────────
    query = st.text_input(
        "Search journal, goals, cycles, history",
        placeholder="min 2 characters...",
        key="mem_search",
    )

    if query and len(query) >= 2:
        with st.spinner("Searching..."):
            results = do_search(query)
        st.subheader(f"Search results for '{query}' ({results.get('count', 0)} found)")
        if not results.get("results"):
            st.info("No results found.")
        else:
            for r in results["results"]:
                source = r.get("source", "")
                icon = {"journal": "📓", "goal": "🎯", "cycle": "🔄", "history": "📜"}.get(source, "•")
                with st.expander(f"{icon} [{source}] {r.get('title', '')[:80]}"):
                    st.write(r.get("snippet", ""))
                    meta = []
                    if r.get("cycle"):
                        meta.append(f"Cycle: {r['cycle']}")
                    if r.get("status"):
                        meta.append(f"Status: {r['status']}")
                    if r.get("timestamp"):
                        meta.append(f"Time: {str(r['timestamp'])[:19]}")
                    if meta:
                        st.caption(" | ".join(meta))
        return  # early-return on active search

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
                cycle = entry.get("cycle", "?")
                goal = entry.get("goal", "Unknown")
                status = entry.get("status", "completed")
                ts = str(entry.get("timestamp", ""))[:19].replace("T", " ")
                status_icon = {"completed": "✅", "failed": "❌", "in_progress": "🔄"}.get(status, "⏳")

                with st.expander(f"{status_icon} Cycle {cycle} — {goal[:60]} `{ts}`"):
                    entry_type = entry.get("type", "")
                    category = entry.get("category", "")
                    actions = entry.get("actions", [])
                    summary = entry.get("summary") or entry.get("outcome", "")
                    if entry_type:
                        st.caption(f"Type: {entry_type}" + (f" · Category: {category}" if category else ""))
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

                status_badge = {"completed": "✅", "failed": "❌", "running": "⏳", "exited": "⚠️"}.get(status, "•")

                col1, col2 = st.columns([4, 1])
                with col1:
                    st.markdown(f"{status_badge} **#{num}** `{cmd}` — {started}{dur_str}")
                detail_key = f"mem_show_log_{num}"
                with col2:
                    if st.button("Details", key=f"mem_log_detail_btn_{i}_{num}"):
                        st.session_state[detail_key] = not st.session_state.get(detail_key, False)

                if st.session_state.get(detail_key, False):
                    detail = load_log_detail(num)
                    if detail:
                        with st.container():
                            st.caption(f"PID: {detail.get('pid')} | Exit code: {detail.get('exit_code', 'N/A')}")
                            tab_out, tab_err = st.tabs(["stdout", "stderr"])
                            with tab_out:
                                stdout = detail.get("stdout", "")
                                st.code(stdout if stdout else "(empty)", language="text")
                            with tab_err:
                                stderr = detail.get("stderr", "")
                                st.code(stderr if stderr else "(empty)", language="text")
                            if st.button("Refresh", key=f"mem_refresh_log_{num}"):
                                load_log_detail.clear()
                                st.rerun()
                    else:
                        st.warning(f"Log #{num} not found.")

        st.divider()

        # Command history
        st.subheader("Command History")
        history = load_history() or []

        if not history:
            st.caption("No command history yet.")
        else:
            type_icons = {"goal": "🎯", "message": "💬", "bash": "💻"}
            for cmd in reversed(history[-30:]):
                cmd_type = cmd.get("type", "?")
                icon = type_icons.get(cmd_type, "•")
                content = (cmd.get("content") or "")[:80]
                cmd_result = cmd.get("result", "")
                ts = str(cmd.get("timestamp", ""))[:19].replace("T", " ")
                st.markdown(f"{icon} **[{cmd_type}]** {content}")
                st.caption(f"{cmd_result} | {ts}")

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
                    c_icon = {"completed": "✅", "failed": "❌", "in_progress": "🔄"}.get(c_status, "⏳")
                    st.markdown(f"{c_icon} **#{cycle_num}**")
                    if cycle_type:
                        st.caption(cycle_type[:12])
                    if dur is not None:
                        st.caption(f"{dur}s")

            with st.expander(f"All {len(cycles)} cycles"):
                rows = []
                for c in reversed(cycles):
                    rows.append({
                        "Cycle": c.get("cycle", ""),
                        "Type": c.get("type", ""),
                        "Status": c.get("status", ""),
                        "Duration (s)": c.get("duration_seconds"),
                        "Goal": (c.get("goal") or "")[:60],
                        "Start": str(c.get("start", ""))[:19].replace("T", " "),
                    })
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
            filtered = goals if selected_status == "all" else [g for g in goals if g.get("status") == selected_status]

            status_icons = {"completed": "✅", "failed": "❌", "in_progress": "🔄", "pending": "⏳"}

            # Summary metrics
            total = len(goals)
            completed = sum(1 for g in goals if g.get("status") == "completed")
            failed = sum(1 for g in goals if g.get("status") == "failed")
            in_progress = sum(1 for g in goals if g.get("status") in ("in_progress", "in-progress"))
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
                created = (g.get("created_at") or g.get("source_timestamp") or "")[:19].replace("T", " ")
                with st.expander(f"{icon} {content[:80]} `{g_status}`"):
                    st.markdown(f"**Goal:** {content}")
                    st.caption(f"Status: {g_status} | Created: {created}")
                    if g.get("id"):
                        st.caption(f"ID: {g['id']}")

    # ── Memory Files sub-tab ──────────────────────────────────
    with tab_memfiles:
        st.subheader("Memory Files")
        memory_files = load_memory_files() or []

        if not memory_files:
            st.caption("No memory files found in /agent/memory/")
        else:
            selected_mem = st.selectbox("Select file", memory_files, key="mem_file_select")
            if selected_mem:
                content = read_memory_file(selected_mem)
                if content is None:
                    st.error(f"Could not read: {selected_mem}")
                elif isinstance(content, dict) and content.get("__type__") == "image":
                    _show_image(content["path"], selected_mem)
                else:
                    if selected_mem.endswith(".json"):
                        st.code(content, language="json")
                    elif selected_mem.endswith(".md"):
                        st.markdown(content)
                    else:
                        st.code(content, language="text")
                    st.caption(f"Size: {len(content)} chars")

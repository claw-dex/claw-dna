"""Tab: Services — background services management and scheduled task administration."""

from datetime import datetime, timezone

import streamlit as st

# Number of trailing log lines to show for stdout/stderr in the service detail
# expander — analogous to `tail -n 200`. The full log is still available via
# the Download buttons.
_LOG_TAIL_LINES = 200


def _tail_lines(text: str, n: int = _LOG_TAIL_LINES) -> tuple[str, int]:
    """Return (last n lines joined, total line count) for the given text."""
    if not text:
        return "", 0
    lines = text.splitlines()
    total = len(lines)
    if total <= n:
        return text if text.endswith("\n") else text, total
    return "\n".join(lines[-n:]), total


@st.fragment(run_every="10s")
def _render_service_logs(name: str) -> None:
    """Render stdout/stderr tails for a service, refreshing every 10s.

    Scoped as a fragment so the log panel polls the on-disk log files at a
    faster cadence than the global 60s portal tick, without rerunning the
    rest of the page (which would collapse the expander and reset tab state).
    """
    from app.data import load_service_logs

    logs = load_service_logs(name)
    st.caption("**Logs**")
    if not logs:
        st.caption("No log files found for this service.")
        return

    stderr_content = logs.get("stderr", "")
    stdout_content = logs.get("stdout", "")
    stderr_path = logs.get("stderr_path", "")
    stdout_path = logs.get("stdout_path", "")

    if stderr_content:
        stderr_tail, stderr_total = _tail_lines(stderr_content)
        truncated = stderr_total > _LOG_TAIL_LINES
        suffix = (
            f" — last {_LOG_TAIL_LINES} of {stderr_total} lines"
            if truncated
            else f" — {stderr_total} line(s)"
        )
        st.markdown(f"**stderr** — errors and crash output{suffix}")
        st.code(stderr_tail, language="log")
        if stderr_path:
            try:
                with open(stderr_path, "r", encoding="utf-8", errors="replace") as _f:
                    _full_stderr = _f.read()
            except OSError:
                _full_stderr = stderr_content
            st.download_button(
                "Download stderr log",
                data=_full_stderr,
                file_name=f"service-{name}.stderr.log",
                mime="text/plain",
                key=f"dl_stderr_{name}",
                width="content",
            )

    if stdout_content:
        stdout_tail, stdout_total = _tail_lines(stdout_content)
        truncated = stdout_total > _LOG_TAIL_LINES
        suffix = (
            f" — last {_LOG_TAIL_LINES} of {stdout_total} lines"
            if truncated
            else f" — {stdout_total} line(s)"
        )
        st.markdown(f"**stdout**{suffix}")
        st.code(stdout_tail, language="log")
        if stdout_path:
            try:
                with open(stdout_path, "r", encoding="utf-8", errors="replace") as _f:
                    _full_stdout = _f.read()
            except OSError:
                _full_stdout = stdout_content
            st.download_button(
                "Download stdout log",
                data=_full_stdout,
                file_name=f"service-{name}.stdout.log",
                mime="text/plain",
                key=f"dl_stdout_{name}",
                width="content",
            )

    if not stderr_content and not stdout_content:
        st.caption("No log output available.")


def render():
    from app.data import (
        load_services_full,
        load_service_logs,
        stop_service,
        start_service,
        remove_service,
        load_scheduled_tasks,
        create_scheduled_task,
        update_scheduled_task,
        delete_scheduled_task,
    )
    from app.shared import _badge

    # ── Services ──────────────────────────────────────────────
    st.subheader("Services")
    services = load_services_full() or {}

    if not services:
        st.caption("No services registered.")
    else:
        for name, svc in services.items():
            alive = svc.get("alive", False)
            pid = svc.get("pid", "?")
            has_command = bool(svc.get("command"))
            icon = "🟢" if alive else "🔴"
            status_color = "#4CAF50" if alive else "#F44336"
            status_label = "running" if alive else "stopped"
            col_name, col_status, col_action = st.columns([3, 2, 1])
            with col_name:
                st.markdown(f"{icon} **{name}** (PID {pid})")
            with col_status:
                st.markdown(_badge(status_label, status_color), unsafe_allow_html=True)
            with col_action:
                del_key = f"confirm_del_svc_{name}"
                if alive:
                    st.session_state.pop(f"svc_start_error_{name}", None)
                    if st.button("Stop", key=f"stop_svc_{name}", type="primary"):
                        result = stop_service(name)
                        if result.get("ok"):
                            st.toast(f"Stopped '{name}'")
                            st.rerun()
                        else:
                            st.error(result.get("error"))
                else:
                    # Show Start and Delete buttons side by side when service is stopped
                    if not st.session_state.get(del_key, False):
                        action_cols = st.columns(2 if has_command else 1)
                        if has_command:
                            with action_cols[0]:
                                if st.button(
                                    "Start", key=f"start_svc_{name}", type="primary"
                                ):
                                    result = start_service(name)
                                    if result.get("ok"):
                                        st.toast(f"Started '{name}'")
                                        st.rerun()
                                    else:
                                        st.session_state[f"svc_start_error_{name}"] = (
                                            result.get("error", "Unknown error")
                                        )
                                        st.rerun()
                            with action_cols[1]:
                                if st.button("Delete", key=f"del_svc_{name}"):
                                    st.session_state[del_key] = True
                                    st.rerun()
                        else:
                            with action_cols[0]:
                                if st.button("Delete", key=f"del_svc_{name}"):
                                    st.session_state[del_key] = True
                                    st.rerun()

            # Delete confirmation banner — full width below the row
            if not alive and st.session_state.get(del_key, False):
                st.warning(
                    f"Delete service '{name}'? This will remove it from the registry."
                )
                dc1, dc2, _ = st.columns([1, 1, 4])
                if dc1.button(
                    "Confirm Delete", key=f"del_svc_yes_{name}", type="primary"
                ):
                    result = remove_service(name)
                    if result.get("ok"):
                        st.session_state.pop(del_key, None)
                        st.toast(f"Deleted '{name}'")
                        st.rerun()
                    else:
                        st.error(result.get("error"))
                if dc2.button("Cancel", key=f"del_svc_no_{name}"):
                    st.session_state.pop(del_key, None)
                    st.rerun()

            # Service start error with Ask AI button
            err_key = f"svc_start_error_{name}"
            if not alive and st.session_state.get(err_key):
                error_msg = st.session_state[err_key]
                st.error(f"Failed to start '{name}':\n\n{error_msg}")
                err_cols = st.columns([2, 2, 4])
                with err_cols[0]:
                    if st.button(
                        "Ask AI for Help", key=f"ask_ai_{name}", type="primary"
                    ):
                        logs = load_service_logs(name)
                        stderr_tail = ""
                        if logs and logs.get("stderr"):
                            stderr_tail = logs["stderr"][-2000:]
                        prompt = (
                            f"The service '{name}' failed to start with the following error:\n\n"
                            f"{error_msg}\n\n"
                        )
                        if stderr_tail:
                            prompt += (
                                f"Service stderr log (last 2 KB):\n\n{stderr_tail}\n\n"
                            )
                        prompt += "Please help me resolve this issue."
                        st.session_state["chat_pending_prompt"] = prompt
                        st.session_state.pop(err_key, None)
                        st.rerun()
                with err_cols[1]:
                    if st.button("Dismiss", key=f"dismiss_err_{name}"):
                        st.session_state.pop(err_key, None)
                        st.rerun()

            # Service detail expander — auto-opens when stopped with stderr content
            logs = load_service_logs(name)
            has_stderr = bool(logs and logs.get("stderr"))
            auto_expand = not alive and has_stderr
            with st.expander(f"Details: {name}", expanded=auto_expand):
                # Metadata row
                port = svc.get("port")
                command = svc.get("command")
                started = svc.get("started", "")
                auto_start = svc.get("auto_start", False)
                started_str = started[:19].replace("T", " ") if started else "—"
                m1, m2, m3, m4 = st.columns(4)
                with m1:
                    st.caption("**Port**")
                    st.markdown(str(port) if port else "—")
                with m2:
                    st.caption("**Started**")
                    st.markdown(started_str)
                with m3:
                    st.caption("**Auto-start**")
                    as_color = "#4CAF50" if auto_start else "#888"
                    st.markdown(
                        _badge("yes" if auto_start else "no", as_color),
                        unsafe_allow_html=True,
                    )
                with m4:
                    st.caption("**PID**")
                    st.markdown(str(pid))
                if command:
                    st.caption("**Command**")
                    st.code(
                        (
                            " ".join(str(c) for c in command)
                            if isinstance(command, list)
                            else str(command)
                        ),
                        language="bash",
                    )

                # Logs — refreshed at 10s via st.fragment, independent of
                # the global 60s portal tick.
                _render_service_logs(name)

    st.divider()

    # ── Scheduled Tasks ───────────────────────────────────────
    st.subheader("Scheduled Tasks")
    sched_tasks = load_scheduled_tasks() or []

    if not sched_tasks:
        st.caption("No scheduled tasks configured.")
    else:
        enabled = [t for t in sched_tasks if t.get("enabled", True)]
        disabled = len(sched_tasks) - len(enabled)
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Total", len(sched_tasks))
        with c2:
            st.metric("Enabled", len(enabled))
        with c3:
            st.metric("Disabled", disabled)

        for idx, task in enumerate(sched_tasks):
            tid = task.get("id", "?")
            ttype = task.get("schedule_type", "?")
            is_enabled = task.get("enabled", True)
            content = task.get("content", "")
            last_run = task.get("last_run")

            if ttype == "interval":
                sched_str = f"every {task.get('interval_minutes', '?')}m"
            elif ttype == "cron":
                sched_str = task.get("schedule", "?") + " (UTC)"
            elif ttype == "once":
                sched_str = task.get("run_at", "?")
            else:
                sched_str = "?"

            icon = "🟢" if is_enabled else "⚫"
            last_str = last_run[:19].replace("T", " ") if last_run else "never"

            col_id, col_sched, col_action = st.columns([3, 3, 2])
            with col_id:
                st.markdown(f"{icon} **{tid}** · `{ttype}`")
            with col_sched:
                st.caption(f"Schedule: {sched_str} | Last: {last_str}")
            with col_action:
                btn_cols = st.columns(2)
                with btn_cols[0]:
                    toggle_label = "Disable" if is_enabled else "Enable"
                    if st.button(toggle_label, key=f"sched_toggle_{idx}"):
                        result = update_scheduled_task(tid, {"enabled": not is_enabled})
                        if result.get("ok"):
                            st.toast(
                                f"{'Disabled' if is_enabled else 'Enabled'} '{tid}'"
                            )
                            st.rerun()
                        else:
                            st.error(result.get("error"))
                with btn_cols[1]:
                    if st.button("Delete", key=f"sched_del_{idx}"):
                        result = delete_scheduled_task(tid)
                        if result.get("ok"):
                            st.toast(f"Deleted '{tid}'")
                            st.rerun()
                        else:
                            st.error(result.get("error"))

            if content:
                st.caption(f"Content: {content[:100]}")

            with st.expander(f"Edit {tid}", expanded=False):
                edit_content = st.text_area(
                    "Content", value=content, key=f"sched_edit_content_{idx}"
                )
                inbox_options = ["goal", "message"]
                current_inbox = task.get("type", "goal")
                inbox_idx = (
                    inbox_options.index(current_inbox)
                    if current_inbox in inbox_options
                    else 0
                )
                edit_inbox_type = st.selectbox(
                    "Inbox Type",
                    inbox_options,
                    index=inbox_idx,
                    key=f"sched_edit_inbox_{idx}",
                )
                e1, e2 = st.columns(2)
                with e1:
                    edit_priority = st.number_input(
                        "Priority (1-5)",
                        min_value=1,
                        max_value=5,
                        value=task.get("priority", 3),
                        key=f"sched_edit_pri_{idx}",
                    )
                with e2:
                    if ttype == "interval":
                        edit_interval = st.number_input(
                            "Interval (minutes)",
                            min_value=1,
                            value=task.get("interval_minutes", 60),
                            key=f"sched_edit_int_{idx}",
                        )
                    elif ttype == "cron":
                        edit_interval = st.text_input(
                            "Cron schedule (UTC)",
                            value=task.get("schedule", ""),
                            key=f"sched_edit_cron_{idx}",
                        )
                    elif ttype == "once":
                        edit_interval = st.text_input(
                            "Run at (ISO datetime)",
                            value=task.get("run_at", ""),
                            key=f"sched_edit_runat_{idx}",
                        )
                    else:
                        edit_interval = None
                if st.button("Save Changes", key=f"sched_save_{idx}", type="primary"):
                    updates = {
                        "content": edit_content,
                        "priority": edit_priority,
                        "type": edit_inbox_type,
                    }
                    if ttype == "interval" and edit_interval is not None:
                        updates["interval_minutes"] = edit_interval
                    elif ttype == "cron" and edit_interval is not None:
                        updates["schedule"] = edit_interval
                    elif ttype == "once" and edit_interval is not None:
                        updates["run_at"] = edit_interval
                    result = update_scheduled_task(tid, updates)
                    if result.get("ok"):
                        st.toast(f"Updated '{tid}'")
                        st.rerun()
                    else:
                        st.error(result.get("error"))

    # ── Create new scheduled task ──
    with st.expander("Create New Scheduled Task", expanded=False):
        new_id = st.text_input(
            "Task ID", placeholder="e.g. daily-check", key="sched_new_id"
        )
        new_sched_type = st.selectbox(
            "Schedule Type", ["interval", "cron", "once"], key="sched_new_sched_type"
        )
        new_inbox_type = st.selectbox(
            "Inbox Type", ["goal", "message"], key="sched_new_inbox_type"
        )
        new_content = st.text_area(
            "Content", placeholder="What the agent should do", key="sched_new_content"
        )
        nc1, nc2 = st.columns(2)
        with nc1:
            new_priority = st.number_input(
                "Priority (1-5)",
                min_value=1,
                max_value=5,
                value=3,
                key="sched_new_pri",
            )
        with nc2:
            if new_sched_type == "interval":
                new_schedule_val = st.number_input(
                    "Interval (minutes)",
                    min_value=1,
                    value=60,
                    key="sched_new_int",
                )
            elif new_sched_type == "cron":
                new_schedule_val = st.text_input(
                    "Cron schedule in UTC (min hour dom mon dow)",
                    placeholder="0 9 * * *",
                    key="sched_new_cron",
                )
            else:
                new_schedule_val = st.text_input(
                    "Run at (ISO datetime)",
                    placeholder="2026-03-10T09:00",
                    value=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
                    key="sched_new_runat",
                )
        if st.button("Create Task", key="sched_create_btn", type="primary"):
            if not new_id or not new_content:
                st.error("Task ID and Content are required.")
            else:
                task_data = {
                    "id": new_id.strip(),
                    "schedule_type": new_sched_type,
                    "type": new_inbox_type,
                    "content": new_content,
                    "enabled": True,
                    "last_run": None,
                    "priority": new_priority,
                }
                if new_sched_type == "interval":
                    task_data["interval_minutes"] = new_schedule_val
                elif new_sched_type == "cron":
                    task_data["schedule"] = new_schedule_val
                else:
                    task_data["run_at"] = new_schedule_val
                result = create_scheduled_task(task_data)
                if result.get("ok"):
                    st.toast(f"Created '{new_id}'")
                    st.rerun()
                else:
                    st.error(result.get("error"))

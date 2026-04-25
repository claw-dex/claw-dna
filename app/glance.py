"""Quick-glance dashboard — compact Goals/Inbox/Outbox summary above the chat."""

import html as _html
from datetime import datetime, timedelta, timezone

import streamlit as st

from app.data import (
    load_goals,
    load_inbox,
    load_inbox_history,
    load_outbox,
    load_scheduled_tasks,
)
from app.data.write import delete_scheduled_task
from app.shared import _badge, _STATUS_COLORS, _TYPE_COLORS, parse_dt

MAX_ITEMS = 10
CONTAINER_HEIGHT = 320


def _time_ago(ts_str):
    """Convert ISO timestamp to relative time string."""
    if not ts_str:
        return ""
    dt = parse_dt(ts_str)
    if dt is None:
        return str(ts_str)[:10]
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _extract_ts(item):
    """Extract the best timestamp string for sorting."""
    return (
        item.get("updated_at") or item.get("created_at") or item.get("timestamp") or ""
    )


def _build_items(goals, inbox, outbox, filter_cat):
    """Build a unified list of compact display items, newest-first."""
    items = []

    if filter_cat in ("All", "Goals"):
        # "All" shows only active goals to save space; "Goals" shows everything
        _active_statuses = ("in_progress", "pending")
        goal_list = (
            goals
            if filter_cat == "Goals"
            else [g for g in goals if g.get("status") in _active_statuses]
        )
        for g in goal_list:
            status = g.get("status", "pending")
            items.append(
                {
                    "cat": "goal",
                    "icon": "🎯",
                    "badge_text": status.replace("_", " ").replace("-", " "),
                    "badge_color": _STATUS_COLORS.get(status, "#666"),
                    "text": g.get("goal") or g.get("content") or "",
                    "ts": _extract_ts(g),
                    "pinned": False,
                    "raw": g,
                }
            )

    if filter_cat in ("All", "Inbox"):
        for item in inbox:
            cmd_t = item.get("type", "?")
            items.append(
                {
                    "cat": "inbox",
                    "icon": "📥",
                    "badge_text": cmd_t,
                    "badge_color": _TYPE_COLORS.get(cmd_t, "#666"),
                    "text": item.get("content") or "",
                    "ts": item.get("timestamp") or "",
                    "pinned": False,
                    "raw": item,
                }
            )

    if filter_cat in ("All", "Outbox"):
        for item in outbox:
            is_human = item.get("type") == "needs_human"
            items.append(
                {
                    "cat": "outbox",
                    "icon": "🔔" if is_human else "📤",
                    "badge_text": (
                        "needs human" if is_human else (item.get("type") or "response")
                    ),
                    "badge_color": "#F44336" if is_human else "#4CAF50",
                    "text": item.get("subject") or item.get("content") or "",
                    "ts": item.get("timestamp") or "",
                    "pinned": is_human,
                    "raw": item,
                }
            )

    # Sort: pinned items first, then newest-first by timestamp
    items.sort(key=lambda x: (x["pinned"], x["ts"] != "", x["ts"]), reverse=True)

    return items


def _render_detail(item):
    """Render full detail content inside an expander."""
    raw = item["raw"]
    badge = _badge(item["badge_text"], item["badge_color"])
    cat = item["cat"]

    if cat == "goal":
        st.markdown(badge, unsafe_allow_html=True)
        goal_text = raw.get("goal") or raw.get("content") or ""
        st.markdown(goal_text)
        source = raw.get("source", "")
        if source:
            st.markdown(_badge(f"source: {source}", "#555"), unsafe_allow_html=True)
        notes = raw.get("notes", "")
        if notes:
            st.info(f"**Notes:** {notes}")
        created_raw = raw.get("created_at", "")
        updated_raw = raw.get("updated_at", "")
        if created_raw:
            st.caption(f"Created: {str(created_raw)[:19].replace('T', ' ')}")
        if updated_raw and updated_raw != created_raw:
            st.caption(f"Updated: {str(updated_raw)[:19].replace('T', ' ')}")

    elif cat == "inbox":
        st.markdown(badge, unsafe_allow_html=True)
        content = raw.get("content") or ""
        st.markdown(content)
        ts = str(raw.get("timestamp", ""))[:19].replace("T", " ")
        if ts:
            st.caption(f"Queued: {ts}")
        priority = raw.get("priority")
        if priority is not None:
            st.caption(f"Priority: {priority}")

    elif cat == "outbox":
        is_human = raw.get("type") == "needs_human"
        subject = raw.get("subject", "")
        content = raw.get("content") or ""
        if is_human:
            st.warning(f"**{subject or 'Action required'}**\n\n{content}")
        else:
            st.markdown(badge, unsafe_allow_html=True)
            if subject:
                st.markdown(f"**{subject}**")
            st.markdown(content)
        ts = str(raw.get("timestamp", ""))[:19].replace("T", " ")
        if ts:
            st.caption(f"Sent: {ts}")


def _next_trigger(task, now):
    """Return (next_dt or None, display_str) for a scheduled task."""
    if not task.get("enabled", True):
        return None, "disabled"

    ttype = task.get("schedule_type", "")
    last_run = parse_dt(task.get("last_run"))

    if ttype == "interval":
        interval_min = task.get("interval_minutes", 0)
        if interval_min <= 0:
            return None, "invalid"
        if last_run is None:
            return now, "due now"
        nxt = last_run + timedelta(minutes=interval_min)
        delta = (nxt - now).total_seconds()
        if delta <= 0:
            return nxt, "overdue"
        if delta < 60:
            return nxt, f"in {int(delta)}s"
        if delta < 3600:
            return nxt, f"in {int(delta // 60)}m"
        if delta < 86400:
            return nxt, f"in {delta / 3600:.1f}h"
        return nxt, f"in {delta / 86400:.1f}d"

    elif ttype == "once":
        if last_run is not None:
            return None, "already ran"
        run_at = parse_dt(task.get("run_at"))
        if not run_at:
            return None, "no run_at"
        delta = (run_at - now).total_seconds()
        if delta <= 0:
            return run_at, "overdue"
        if delta < 60:
            return run_at, f"in {int(delta)}s"
        if delta < 3600:
            return run_at, f"in {int(delta // 60)}m"
        if delta < 86400:
            return run_at, f"in {delta / 3600:.1f}h"
        return run_at, f"in {delta / 86400:.1f}d"

    elif ttype == "cron":
        schedule = task.get("schedule", "?")
        return None, f"cron {schedule}"

    return None, "?"


def _render_upcoming_tasks():
    """Render up to 5 upcoming scheduled tasks sorted by next trigger time."""
    tasks = load_scheduled_tasks() or []
    if not tasks:
        return

    now = datetime.now(timezone.utc)
    upcoming = []
    for t in tasks:
        if not t.get("enabled", True):
            continue
        nxt_dt, nxt_str = _next_trigger(t, now)
        upcoming.append((nxt_dt, nxt_str, t))

    # Sort: tasks with a concrete next_dt first (by time), then cron (no dt) last
    upcoming.sort(key=lambda x: (x[0] is None, x[0] or now))
    upcoming = upcoming[:5]

    if not upcoming:
        return

    st.markdown(
        f"**Upcoming Tasks** &nbsp; ⏱ {len(upcoming)}",
        unsafe_allow_html=True,
    )
    rows_html = []
    for _nxt_dt, nxt_str, t in upcoming:
        tid = _html.escape(str(t.get("id", "?")))
        content = _html.escape((t.get("content") or "")[:60])
        ttype = _html.escape(str(t.get("schedule_type", "?")))
        time_color = (
            "#F44336" if "overdue" in nxt_str or "due now" in nxt_str else "#888"
        )
        rows_html.append(
            f"<tr>"
            f'<td style="padding:2px 6px;font-size:12px"><code>{tid}</code></td>'
            f'<td style="padding:2px 6px;font-size:11px;color:#aaa">{content}</td>'
            f'<td style="padding:2px 6px;font-size:11px;color:#888">{ttype}</td>'
            f'<td style="padding:2px 6px;font-size:11px;color:{time_color};text-align:right;white-space:nowrap">{_html.escape(nxt_str)}</td>'
            f"</tr>"
        )
    st.markdown(
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:4px">'
        f'<thead><tr style="border-bottom:1px solid #333">'
        f'<th style="padding:2px 6px;font-size:10px;color:#666;text-align:left">ID</th>'
        f'<th style="padding:2px 6px;font-size:10px;color:#666;text-align:left">Content</th>'
        f'<th style="padding:2px 6px;font-size:10px;color:#666;text-align:left">Schedule</th>'
        f'<th style="padding:2px 6px;font-size:10px;color:#666;text-align:right">Next</th>'
        f"</tr></thead>"
        f'<tbody>{"".join(rows_html)}</tbody>'
        f"</table>",
        unsafe_allow_html=True,
    )


def _collect_reminder_rows():
    """Build the deduped reminder list shown in the Reminders section.

    Joins inbox + inbox_history entries (by task_id) against
    scheduled_tasks.json entries with source=='reminder'. Only reminders
    that have at least one matching inbox/history entry are returned —
    pending reminders that have not yet fired are omitted by design.
    Returns one row per task_id, carrying the most recent fire timestamp.
    """
    tasks = load_scheduled_tasks() or []
    reminder_tasks = {
        t.get("id"): t
        for t in tasks
        if isinstance(t, dict) and t.get("source") == "reminder" and t.get("id")
    }
    if not reminder_tasks:
        return []

    latest = {}  # task_id -> (sort_key_dt, raw_ts, entry)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for src in (load_inbox() or [], load_inbox_history() or []):
        for it in src:
            if not isinstance(it, dict):
                continue
            tid = it.get("task_id")
            if tid not in reminder_tasks:
                continue
            raw_ts = it.get("timestamp") or ""
            sort_dt = parse_dt(raw_ts) or epoch
            if tid not in latest or sort_dt > latest[tid][0]:
                latest[tid] = (sort_dt, raw_ts, it)

    rows = []
    for tid, (sort_dt, raw_ts, entry) in latest.items():
        task = reminder_tasks[tid]
        rows.append(
            {
                "task_id": tid,
                "content": task.get("content") or entry.get("content") or "",
                "ts": raw_ts,
                "sort_dt": sort_dt,
                "schedule_type": task.get("schedule_type", ""),
            }
        )
    rows.sort(key=lambda r: r["sort_dt"], reverse=True)
    return rows


def _render_reminders():
    """Render the 🔔 Reminders section with a Done button per row."""
    rows = _collect_reminder_rows()
    st.markdown("**🔔 Reminders**")
    if not rows:
        st.caption("No active reminders.")
        return
    for r in rows:
        col_text, col_btn = st.columns([5, 1])
        with col_text:
            raw_preview = r["content"][:100] + (
                "..." if len(r["content"]) > 100 else ""
            )
            preview = _html.escape(raw_preview)
            time_str = _html.escape(_time_ago(r["ts"]))
            st.markdown(
                f"{preview} &nbsp; · &nbsp; <em>{time_str}</em>",
                unsafe_allow_html=True,
            )
        with col_btn:
            if st.button("Done", key=f"rem_done_{r['task_id']}"):
                result = delete_scheduled_task(r["task_id"]) or {}
                if result.get("ok"):
                    st.toast(f"Reminder cleared: {r['task_id']}")
                else:
                    st.toast(
                        f"Failed to clear reminder: {result.get('error', 'unknown error')}",
                        icon="⚠️",
                    )
                st.rerun()


def render():
    goals = load_goals() or []
    inbox = load_inbox() or []
    outbox = load_outbox() or []

    active_goals = [g for g in goals if g.get("status") in ("in_progress", "pending")]
    needs_human = [o for o in outbox if o.get("type") == "needs_human"]

    # ── Header row: title + counts | filter ──
    col_title, col_filter = st.columns([2, 3])
    with col_title:
        parts = [
            f"**Quick Glance** &nbsp; 🎯 {len(active_goals)} &nbsp; 📥 {len(inbox)} &nbsp; 📤 {len(outbox)}"
        ]
        if needs_human:
            parts.append(f"&nbsp; 🔔 {len(needs_human)}")
        st.markdown("".join(parts), unsafe_allow_html=True)
    with col_filter:
        filter_cat = st.segmented_control(
            "Filter",
            ["All", "Goals", "Inbox", "Outbox", "Upcoming"],
            default="All",
            key="glance_filter",
            label_visibility="collapsed",
        )
        if filter_cat is None:
            filter_cat = "All"

    _render_reminders()

    if filter_cat == "Upcoming":
        _render_upcoming_tasks()
        return

    items = _build_items(goals, inbox, outbox, filter_cat)

    if not items:
        st.caption("Nothing to show.")
        return

    # ── Render clickable rows inside fixed-height scrollable container ──
    display_items = items[:MAX_ITEMS]
    overflow = len(items) - MAX_ITEMS

    with st.container(height=CONTAINER_HEIGHT, border=True):
        for item in display_items:
            preview = item["text"][:80] + ("..." if len(item["text"]) > 80 else "")
            time_str = _time_ago(item["ts"])
            pin_marker = "🔴 " if item["pinned"] else ""
            label = f"{pin_marker}{item['icon']} {item['badge_text']}  ·  {preview}  ·  {time_str}"

            with st.expander(label, expanded=item["pinned"]):
                _render_detail(item)

        if overflow > 0:
            st.caption(f"... and {overflow} more — see Command Center for full details")

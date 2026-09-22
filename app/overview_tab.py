"""Tab 5: Overview — suggestions, goal performance, evolution balance, health.

Every number on this page is read pre-computed from the local DuckDB metrics store
(see ``scripts/metrics_db.py`` and ``app/data/metrics.py``). This module is pure
presentation: it formats and renders, it never aggregates.
"""

import streamlit as st
from datetime import datetime, timezone, timedelta

from app.shared import (
    _STATUS_COLORS,
    _badge,
    heartbeat_freshness,
)

_CATEGORY_COLORS = {
    "capability": "#2196F3",
    "observability": "#9C27B0",
    "reliability": "#4CAF50",
    "efficiency": "#FF9800",
    "prompt_evolution": "#E91E63",
    "memory_consolidation": "#673AB7",
    "deep_sleep": "#3F51B5",
}
_CATEGORY_ICONS = {
    "capability": "⚡",
    "observability": "👁️",
    "reliability": "🛡️",
    "efficiency": "⏩",
    "prompt_evolution": "✏️",
    "memory_consolidation": "🧠",
    "deep_sleep": "💤",
}
_TYPE_ICONS = {
    "bootstrap": "🌱",
    "evolve": "🧬",
    "goal": "🎯",
    "self-heal": "🔧",
    "dream": "💤",
}
_CYCLE_TYPE_COLORS = {
    "evolve": "#4CAF50",
    "goal": "#2196F3",
    "bootstrap": "#FF9800",
    "self-heal": "#F44336",
    "dream": "#3F51B5",
}


def _fmt_duration(seconds):
    """Compact duration label: '45s', '3m20s', '1h 5m'."""
    if not seconds:
        return ""
    secs = int(seconds)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


def _render_today_glance(now_utc):
    """Render a compact daily activity summary — cycles, time, categories, summaries."""
    from app.data.metrics import load_day_glance

    today_str = now_utc.strftime("%Y-%m-%d")
    yesterday_str = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")

    day = load_day_glance(today_str, yesterday_str)
    today_completed = day["cycles_completed"]
    yesterday_completed = day["prev_completed"]
    today_secs = day["active_seconds"]
    today_cats = day["categories"]

    st.subheader("Today at a Glance")
    st.caption(f"Activity summary for {today_str} (UTC)")

    # Metrics row
    delta_cycles = (
        today_completed - yesterday_completed if yesterday_completed else None
    )
    delta_str = f"{delta_cycles:+d} vs yesterday" if delta_cycles is not None else None

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric(
            "Cycles Today",
            today_completed,
            delta=delta_str,
        )
    with m2:
        hrs = int(today_secs // 3600)
        mins = int((today_secs % 3600) // 60)
        secs = int(today_secs % 60)
        if hrs:
            time_str = f"{hrs}h {mins}m"
        elif mins:
            time_str = f"{mins}m {secs}s"
        else:
            time_str = f"{int(today_secs)}s"
        st.metric("Time Active Today", time_str)
    with m3:
        evolve_today = day["evolve_cycles"]
        st.metric(
            "Goals Run",
            day["goal_cycles"],
            delta=f"{evolve_today} evolve" if evolve_today else None,
        )
    with m4:
        cat_list = ", ".join(
            f"{_CATEGORY_ICONS.get(c,'•')} {c.replace('_',' ')}"
            for c in sorted(today_cats, key=lambda k: -today_cats[k])
        )
        st.metric(
            "Categories",
            day["category_count"],
            help=cat_list if cat_list else "No categories today",
        )

    timeline = day["timeline"]
    if not timeline:
        st.info("No cycles yet today — agent starts fresh each heartbeat.")
        return

    items_html = []
    for c in timeline:
        cn = c.get("cycle_number")
        cn = "?" if cn is None else cn
        ctype = c.get("cycle_type") or "?"
        cat = c.get("cycle_category") or ""
        status = c.get("cycle_status") or ""
        start_ts = str(c.get("start_ts") or "")[11:16]  # HH:MM
        summary = (c.get("summary") or "")[:90]

        type_icon = _TYPE_ICONS.get(ctype, "•")
        cat_icon = _CATEGORY_ICONS.get(cat, "") if cat else ""
        status_icon = {"completed": "✅", "failed": "❌", "in_progress": "🔄"}.get(
            status, "⏳"
        )
        cat_color = _CATEGORY_COLORS.get(cat, "#666")
        dur_str = _fmt_duration(c.get("duration_seconds"))

        cat_badge = (
            (
                f'<span style="background:{cat_color};color:#fff;padding:0 5px;'
                f'border-radius:8px;font-size:10px;font-weight:600;margin:0 3px">'
                f'{cat_icon} {cat.replace("_"," ")}</span>'
            )
            if cat
            else ""
        )

        status_color = _STATUS_COLORS.get(status, "#666")
        status_badge = (
            f'<span style="background:{status_color};color:#fff;padding:0 5px;'
            f'border-radius:8px;font-size:10px;font-weight:600;margin:0 3px">'
            f'{status.replace("_"," ") if status else "?"}</span>'
        )

        items_html.append(
            f'<div style="display:flex;align-items:flex-start;gap:6px;padding:5px 0;'
            f'border-bottom:1px solid #222">'
            f'<span style="color:#666;font-size:11px;min-width:36px;padding-top:2px">{start_ts}</span>'
            f'<span style="font-size:14px">{status_icon}{type_icon}</span>'
            f'<div style="flex:1;min-width:0">'
            f'<span style="font-size:12px;font-weight:600">#{cn} {ctype}</span>'
            f"{status_badge}"
            f"{cat_badge}"
            f'<span style="color:#888;font-size:11px;margin-left:4px">{dur_str}</span>'
            f'<div style="color:#bbb;font-size:11px;margin-top:1px;white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis">{summary}</div>'
            f"</div>"
            f"</div>"
        )

    st.markdown(
        f'<div style="max-height:280px;overflow-y:auto;border:1px solid #333;'
        f'border-radius:6px;padding:6px 10px;font-family:monospace">'
        f'{"".join(items_html)}'
        f"</div>",
        unsafe_allow_html=True,
    )

    # Category breakdown for today (if any)
    if today_cats:
        cat_parts = []
        for cat, count in sorted(today_cats.items(), key=lambda x: -x[1]):
            color = _CATEGORY_COLORS.get(cat, "#666")
            icon = _CATEGORY_ICONS.get(cat, "•")
            cat_parts.append(
                f'<span style="display:inline-flex;align-items:center;gap:3px;'
                f"background:{color}22;border:1px solid {color}55;"
                f'border-radius:10px;padding:1px 8px;font-size:11px;margin:2px">'
                f'{icon} {cat.replace("_", " ")} <strong>{count}</strong></span>'
            )
        st.markdown(
            f'<div style="margin-top:6px">{"".join(cat_parts)}</div>',
            unsafe_allow_html=True,
        )


def render():
    from app.data.metrics import (
        load_health,
        load_suggestions,
        load_balance_metrics,
        load_goal_metrics,
        load_velocity_metrics,
        load_improvements,
    )

    st.markdown("## Overview")
    st.caption(
        "Suggestions, goal performance, evolution balance, and health — all in one place."
    )

    # ── Live health strip ──────────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    health = load_health()
    hb_age, hb_icon = heartbeat_freshness(health["last_heartbeat"])
    error_count = health["errors_24h"]

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric(
            "Agent Status",
            f"{_status_icon(health['agent_status'])} {health['agent_status']}",
        )
    with col2:
        st.metric("Heartbeat", f"{hb_icon} {hb_age}")
    with col3:
        st.metric("Cycle #", health["cycle_number"])
    with col4:
        st.metric("Active Goals", health["active_goals"])
    with col5:
        err_label = f"🔴 {error_count}" if error_count else "🟢 0"
        st.metric("Tab Errors (24h)", err_label)

    # Alert banners
    if error_count:
        affected = ", ".join(health["error_tabs"]) or "?"
        st.warning(
            f"**{error_count} tab error(s)** in last 24h — affected: {affected}. See ⚙️ System tab."
        )

    if health["inbox_count"] > 0:
        st.info(
            f"**{health['inbox_count']} message(s)** in inbox — agent will process on next heartbeat."
        )

    st.divider()

    # ── Today at a Glance ─────────────────────────────────────
    _render_today_glance(now_utc)

    st.divider()

    # ── Two-column layout ─────────────────────────────────────
    left, right = st.columns([1, 1])

    with left:
        # ── Suggestions ───────────────────────────────────────
        st.subheader("Suggestions")
        suggestions = load_suggestions()
        if not suggestions:
            st.success("No suggestions — agent is on track!")
        for s in suggestions:
            priority = s.get("priority", "low")
            icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(priority, "⚪")
            with st.expander(f"{icon} {s.get('action', '')[:70]}"):
                st.caption(
                    f"**Priority:** {priority} | **Category:** {s.get('category', '')}"
                )
                st.write(s.get("reason", ""))

    with right:
        # ── Evolution balance ──────────────────────────────────
        st.subheader("Evolution Balance")
        balance = load_balance_metrics()
        rows = balance["rows"]
        total = balance["total_evolve_cycles"]
        suggestion_cat = balance["suggestion"]

        if total == 0 and not balance["has_weights"]:
            st.caption("No evolve cycles yet.")
        elif balance["has_weights"]:
            # Dynamic weights view — show score-based bars
            max_score = balance["max_score"]
            for row in rows:
                cat = row["category"]
                score = row["score"]
                icon = _CATEGORY_ICONS.get(cat, "")
                label = cat.replace("_", " ").title()
                suffix = " ★" if row["is_suggested"] else ""
                count = row["all_time_count"]
                pct = max(0.0, min(1.0, score / max_score)) if max_score > 0 else 0
                st.progress(
                    pct,
                    text=f"{icon} {label}{suffix}: score {score:g} ({count} cycles)",
                )
                # Show signal breakdown in small text
                parts = []
                for field, label_text in (
                    ("base_need", "need"),
                    ("recency_boost", "recency"),
                    ("goal_alignment", "goals"),
                    ("roi_bonus", "roi"),
                ):
                    if row[field] > 0:
                        parts.append(f"{label_text}={row[field]:g}")
                if row["maturity_penalty"] > 0:
                    parts.append(f"maturity=-{row['maturity_penalty']:g}")
                if parts:
                    st.caption("  ".join(parts))
            if suggestion_cat:
                st.info(f"Next focus: **{suggestion_cat.replace('_', ' ').title()}**")
            for gs in balance["goal_signals"][:3]:
                aligned = ", ".join(gs.get("aligned_categories", []))
                st.caption(f"🎯 \"{gs.get('goal', '')[:60]}\" → {aligned}")
        else:
            # Fallback: count-based bars (no weights file yet)
            for row in rows:
                count = row["all_time_count"]
                pct = count / total if total > 0 else 0
                label = row["category"].replace("_", " ").title()
                suffix = " 💡" if row["is_suggested"] else ""
                st.progress(pct, text=f"{label}{suffix}: {count}/{total}")
            if suggestion_cat:
                st.info(f"Next focus: **{suggestion_cat.replace('_', ' ')}**")

    st.divider()

    # ── Goal performance ──────────────────────────────────────
    st.subheader("Goal Performance")
    goal_stats = load_goal_metrics()

    gs1, gs2, gs3, gs4 = st.columns(4)
    with gs1:
        st.metric("Total Goals", goal_stats["total"])
    with gs2:
        st.metric("Completed", goal_stats["completed"])
    with gs3:
        st.metric("Failed", goal_stats["failed"])
    with gs4:
        rate_pct = round(goal_stats["completion_rate"] * 100)
        st.metric("Completion Rate", f"{rate_pct}%")

    if goal_stats["total"] > 0:
        st.progress(
            goal_stats["completion_rate"],
            text=f"{goal_stats['completed']} of {goal_stats['total']} goals completed",
        )

    avg_dur = goal_stats.get("avg_goal_duration_seconds")
    if avg_dur is not None:
        mins = int(avg_dur // 60)
        secs = int(avg_dur % 60)
        dur_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        st.caption(f"Avg goal cycle duration: **{dur_str}**")

    # Goal sparkline — most recent goal cycles, oldest to newest
    durations = goal_stats.get("goal_cycle_durations", [])
    if len(durations) >= 2:
        max_dur = max(d for _, d in durations) or 1
        n = len(durations)
        bar_w, gap, svg_h = 18, 3, 60
        svg_w = n * (bar_w + gap) + 20
        parts = [
            f'<svg width="{svg_w}" height="{svg_h}" xmlns="http://www.w3.org/2000/svg">'
        ]
        for i, (cn, dur) in enumerate(durations):
            cn = "?" if cn is None else cn
            x = 10 + i * (bar_w + gap)
            bar_h = max(4, int(dur / max_dur * 38))
            y = svg_h - bar_h - 14
            parts.append(
                f'<rect x="{x}" y="{y}" width="{bar_w}" height="{bar_h}" '
                f'fill="#2196F3" rx="2" opacity="0.8">'
                f"<title>Cycle {cn}: {int(dur)}s</title></rect>"
            )
            parts.append(
                f'<text x="{x + bar_w//2}" y="{svg_h - 2}" text-anchor="middle" '
                f'font-size="9" fill="#888">{cn}</text>'
            )
        parts.append("</svg>")
        st.markdown(
            f'<div style="overflow-x:auto;margin-top:4px">'
            f'{"".join(parts)}'
            f'<div><small style="color:#888">Goal cycle durations (seconds) — '
            f"last {n} goal cycles</small></div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # Recent goals mini-list
    recent = goal_stats.get("recent_goals", [])
    if recent:
        with st.expander("Recent Goals", expanded=False):
            status_icons = {
                "completed": "✅",
                "failed": "❌",
                "in_progress": "🔄",
                "pending": "⏳",
            }
            for g in recent:
                gstatus = g.get("status") or ""
                icon = status_icons.get(gstatus, "•")
                content = g.get("content") or ""
                created = (g.get("created_at") or "")[:10]
                badge = _badge(
                    gstatus.replace("_", " ") if gstatus else "?",
                    _STATUS_COLORS.get(gstatus, "#666"),
                )
                st.markdown(
                    f"{icon} {badge} &nbsp; **{content[:80]}** — {created}",
                    unsafe_allow_html=True,
                )

    st.divider()

    # ── Cycle velocity chart ───────────────────────────────────
    st.subheader("Cycle Velocity")
    velocity = load_velocity_metrics()
    recent_cycles = velocity["rows"]

    if len(recent_cycles) >= 3:
        durs = [c["duration_seconds"] for c in recent_cycles]
        cycle_nums = [
            "?" if c.get("cycle_number") is None else c["cycle_number"]
            for c in recent_cycles
        ]

        max_dur = max(durs) or 1
        n = len(durs)
        bar_w, gap, svg_h = 16, 3, 70
        svg_w = n * (bar_w + gap) + 20

        parts = [
            f'<svg width="{svg_w}" height="{svg_h + 20}" xmlns="http://www.w3.org/2000/svg">'
        ]
        for i, (dur, cn, c) in enumerate(zip(durs, cycle_nums, recent_cycles)):
            x = 10 + i * (bar_w + gap)
            bar_h = max(4, int(dur / max_dur * 50))
            y = svg_h - bar_h
            color = _CYCLE_TYPE_COLORS.get(c.get("cycle_type", ""), "#9E9E9E")
            dur_label = f"{int(dur)}s" if dur < 60 else f"{int(dur)//60}m"
            parts.append(
                f'<rect x="{x}" y="{y}" width="{bar_w}" height="{bar_h}" '
                f'fill="{color}" rx="2" opacity="0.8">'
                f'<title>Cycle {cn} ({c.get("cycle_type") or "?"}): {dur_label}</title></rect>'
            )
            parts.append(
                f'<text x="{x + bar_w//2}" y="{svg_h + 12}" text-anchor="middle" '
                f'font-size="8" fill="#888">{cn}</text>'
            )
        parts.append("</svg>")

        # Legend
        legend_parts = []
        for ctype, color in _CYCLE_TYPE_COLORS.items():
            legend_parts.append(
                f'<span style="display:inline-flex;align-items:center;margin-right:12px">'
                f'<span style="width:10px;height:10px;background:{color};display:inline-block;'
                f'border-radius:2px;margin-right:4px"></span>'
                f'<span style="font-size:11px;color:#888">{ctype}</span></span>'
            )

        st.markdown(
            f'<div style="overflow-x:auto;padding:4px 0">'
            f'{"".join(parts)}'
            f"</div>"
            f'<div style="margin-top:4px">{"".join(legend_parts)}</div>'
            f'<div><small style="color:#888">Cycle duration (seconds) by cycle number — last {n} completed cycles</small></div>',
            unsafe_allow_html=True,
        )

        # Summary stats
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            if velocity["avg_all"] is not None:
                st.metric("Avg Duration (all)", f"{velocity['avg_all']}s")
        with sc2:
            if velocity["avg_evolve"] is not None:
                st.metric("Avg Evolve", f"{velocity['avg_evolve']}s")
        with sc3:
            if velocity["avg_goal"] is not None:
                st.metric("Avg Goal", f"{velocity['avg_goal']}s")
    else:
        st.caption("Need 3+ completed cycles for velocity chart.")

    st.divider()

    # ── Recent Improvements changelog ─────────────────────────
    st.subheader("Recent Improvements")
    st.caption("What the agent has built and improved — latest first.")

    # Show selector for how many to display
    show_n = st.select_slider(
        "Show last N improvements",
        options=[5, 10, 20, 50],
        value=10,
        key="ov_improvements_n",
    )
    improvement_cycles = load_improvements(show_n)

    if not improvement_cycles:
        st.info("No completed evolve or goal cycles yet.")
    else:
        for c in improvement_cycles:
            cycle_num = c.get("cycle_number")
            cycle_num = "?" if cycle_num is None else cycle_num
            ctype = c.get("cycle_type") or "evolve"
            category = c.get("cycle_category") or ""
            start = str(c.get("start_ts") or "")[:16].replace("T", " ")
            summary = c.get("summary") or ""
            actions = c.get("actions") or []

            # Build category badge HTML
            cat_color = _CATEGORY_COLORS.get(category, "#888")
            cat_icon = _CATEGORY_ICONS.get(category, "•")
            cat_badge = (
                (
                    f'<span style="background:{cat_color};color:#fff;padding:1px 7px;'
                    f'border-radius:10px;font-size:11px;font-weight:600;margin-left:6px">'
                    f'{cat_icon} {category.replace("_", " ")}</span>'
                )
                if category
                else ""
            )

            type_badge_color = "#2196F3" if ctype == "goal" else "#9C27B0"
            type_icon = "🎯" if ctype == "goal" else "🧬"
            type_badge = (
                f'<span style="background:{type_badge_color};color:#fff;padding:1px 7px;'
                f'border-radius:10px;font-size:11px;font-weight:600">'
                f"{type_icon} {ctype}</span>"
            )

            dur_str = _fmt_duration(c.get("duration_seconds"))
            dur_str = f"⏱ {dur_str}  " if dur_str else ""

            label_html = (
                f'<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">'
                f'<span style="font-weight:600">#{cycle_num}</span>'
                f"{type_badge}{cat_badge}"
                f'<span style="color:#888;font-size:12px;margin-left:auto">'
                f"{dur_str}{start}</span>"
                f"</div>"
            )

            # Show summary as expander label (plain text for expander, rich HTML inside)
            short_summary = (summary or "No summary available")[:80]
            expander_label = (
                f"#{cycle_num} · {type_icon} {ctype}"
                + (f" · {cat_icon} {category.replace('_',' ')}" if category else "")
                + f"  — {short_summary}"
            )

            with st.expander(expander_label, expanded=False):
                st.markdown(label_html, unsafe_allow_html=True)
                st.caption(f"Started: {start}")
                if summary:
                    st.markdown(summary)
                if actions:
                    st.markdown("**Actions taken:**")
                    for act in actions[:10]:
                        st.markdown(f"- {act}")
                if not summary and not actions:
                    st.caption("(No detailed summary recorded)")


def _status_icon(status):
    return {
        "idle": "🟡",
        "running": "🟢",
        "healing": "🔴",
        "bootstrapping": "🔵",
        "awaiting_first_heartbeat": "⚪",
    }.get(status, "⚪")

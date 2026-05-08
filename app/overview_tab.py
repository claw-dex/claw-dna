"""Tab 5: Overview — suggestions, goal performance, evolution balance, health."""

import streamlit as st
from datetime import datetime, timezone, timedelta

from app.shared import heartbeat_freshness, parse_dt as _safe_fromisoformat

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


def _render_today_glance(now_utc):
    """Render a compact daily activity summary — cycles, time, categories, summaries."""
    from app.data import load_cycles, load_journal

    today_str = now_utc.strftime("%Y-%m-%d")
    yesterday_dt = now_utc - timedelta(days=1)
    yesterday_str = yesterday_dt.strftime("%Y-%m-%d")

    all_cycles = load_cycles() or []

    # Partition cycles by day
    today_cycles = [c for c in all_cycles if str(c.get("start", ""))[:10] == today_str]
    yesterday_cycles = [
        c for c in all_cycles if str(c.get("start", ""))[:10] == yesterday_str
    ]

    today_secs = sum(c.get("duration_seconds") or 0 for c in today_cycles)
    today_completed = sum(
        1 for c in today_cycles if c.get("cycle_status") == "completed"
    )
    yesterday_completed = len(
        [c for c in yesterday_cycles if c.get("cycle_status") == "completed"]
    )

    # Category breakdown for today
    today_cats: dict[str, int] = {}
    today_types: dict[str, int] = {}
    for c in today_cycles:
        cat = c.get("cycle_category", "")
        if cat:
            today_cats[cat] = today_cats.get(cat, 0) + 1
        ctype = c.get("cycle_type", "unknown")
        today_types[ctype] = today_types.get(ctype, 0) + 1

    # Load journal entries for cycle summaries (journals have richer text than cycles.json)
    journal_data = load_journal(limit=100, offset=0)
    journal_entries = (
        journal_data.get("entries", []) if isinstance(journal_data, dict) else []
    )

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
        goal_today = today_types.get("goal", 0)
        evolve_today = today_types.get("evolve", 0)
        st.metric(
            "Goals Run",
            goal_today,
            delta=f"{evolve_today} evolve" if evolve_today else None,
        )
    with m4:
        cats_today = len(today_cats)
        cat_list = ", ".join(
            f"{_CATEGORY_ICONS.get(c,'•')} {c.replace('_',' ')}"
            for c in sorted(today_cats, key=lambda k: -today_cats[k])
        )
        st.metric(
            "Categories",
            cats_today,
            help=cat_list if cat_list else "No categories today",
        )

    if not today_cycles:
        st.info("No cycles yet today — agent starts fresh each heartbeat.")
        return

    # Compact chronological summary list
    today_sorted = sorted(today_cycles, key=lambda c: c.get("start", ""))

    # Build journal lookup by cycle number
    journal_by_cycle = {
        int(je.get("cycle_number", -1)): je
        for je in journal_entries
        if je.get("cycle_number") is not None
    }

    items_html = []
    for c in today_sorted:
        cn = c.get("cycle_number", "?")
        ctype = c.get("cycle_type", "?")
        cat = c.get("cycle_category", "")
        status = c.get("cycle_status", "")
        dur = c.get("duration_seconds")
        start_ts = str(c.get("start", ""))[11:16]  # HH:MM

        # Prefer journal summary (richer)
        je = journal_by_cycle.get(int(cn) if str(cn).isdigit() else -1, {})
        summary = (je.get("summary") or je.get("outcome") or c.get("summary") or "")[
            :90
        ]

        type_icon = {
            "bootstrap": "🌱",
            "evolve": "🧬",
            "goal": "🎯",
            "self-heal": "🔧",
            "dream": "💤",
        }.get(ctype, "•")
        cat_icon = _CATEGORY_ICONS.get(cat, "") if cat else ""
        status_icon = {"completed": "✅", "failed": "❌", "in_progress": "🔄"}.get(
            status, "⏳"
        )
        cat_color = _CATEGORY_COLORS.get(cat, "#666")

        dur_str = ""
        if dur:
            dur_str = (
                f"{int(dur)}s" if dur < 60 else f"{int(dur)//60}m{int(dur)%60:02d}s"
            )

        cat_badge = (
            (
                f'<span style="background:{cat_color};color:#fff;padding:0 5px;'
                f'border-radius:8px;font-size:10px;font-weight:600;margin:0 3px">'
                f'{cat_icon} {cat.replace("_"," ")}</span>'
            )
            if cat
            else ""
        )

        items_html.append(
            f'<div style="display:flex;align-items:flex-start;gap:6px;padding:5px 0;'
            f'border-bottom:1px solid #222">'
            f'<span style="color:#666;font-size:11px;min-width:36px;padding-top:2px">{start_ts}</span>'
            f'<span style="font-size:14px">{status_icon}{type_icon}</span>'
            f'<div style="flex:1;min-width:0">'
            f'<span style="font-size:12px;font-weight:600">#{cn} {ctype}</span>'
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
    from app.data import (
        load_state,
        load_goals,
        load_inbox,
        load_suggest,
        load_balance,
        load_errors,
        load_goal_stats,
        load_cycles,
        load_journal,
    )

    state = load_state() or {}
    goals = load_goals() or []
    inbox = load_inbox() or []
    errors = load_errors() or []

    st.markdown("## Overview")
    st.caption(
        "Suggestions, goal performance, evolution balance, and health — all in one place."
    )

    # ── Live health strip ──────────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    recent_errors = [
        e
        for e in errors
        if (lambda ts: ts and (now_utc - ts) < timedelta(hours=24))(
            _safe_fromisoformat(e.get("timestamp", ""))
        )
    ]

    active_goals = [g for g in goals if g.get("status") in ("in_progress", "pending")]
    hb_str = state.get("last_heartbeat", "—")
    hb_age, hb_icon = heartbeat_freshness(hb_str)

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric(
            "Agent Status",
            f"{_status_icon(state.get('agent_status', 'unknown'))} {state.get('agent_status', 'unknown')}",
        )
    with col2:
        st.metric("Heartbeat", f"{hb_icon} {hb_age}")
    with col3:
        st.metric("Cycle #", state.get("cycle_number", 0))
    with col4:
        st.metric("Active Goals", len(active_goals))
    with col5:
        err_label = f"🔴 {len(recent_errors)}" if recent_errors else "🟢 0"
        st.metric("Tab Errors (24h)", err_label)

    # Alert banners
    if recent_errors:
        affected = sorted(set(e.get("tab", "?") for e in recent_errors))
        st.warning(
            f"**{len(recent_errors)} tab error(s)** in last 24h — affected: {', '.join(affected)}. See ⚙️ System tab."
        )

    if len(inbox) > 0:
        st.info(
            f"**{len(inbox)} message(s)** in inbox — agent will process on next heartbeat."
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
        suggestions = load_suggest() or []
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
        balance = load_balance()
        all_cats = balance.get("all_cats", [])
        all_time = balance.get("all_time", {})
        total = balance.get("total_evolve_cycles", 0)
        suggestion_cat = balance.get("suggestion")
        weights = balance.get("weights", {})
        goal_signals = balance.get("goal_signals", [])

        if total == 0 and not weights:
            st.caption("No evolve cycles yet.")
        elif weights:
            # Dynamic weights view — show score-based bars
            raw_max = max((w.get("score", 0) for w in weights.values()), default=1)
            max_score = max(raw_max, 1)  # ensure positive denominator
            for cat in all_cats:
                w = weights.get(cat, {})
                score = w.get("score", 0)
                icon = _CATEGORY_ICONS.get(cat, "")
                label = cat.replace("_", " ").title()
                is_suggested = suggestion_cat and cat == suggestion_cat
                count = all_time.get(cat, 0)
                suffix = " ★" if is_suggested else ""
                pct = max(0.0, min(1.0, score / max_score)) if max_score > 0 else 0
                st.progress(
                    pct, text=f"{icon} {label}{suffix}: score {score} ({count} cycles)"
                )
                # Show signal breakdown in small text
                parts = []
                if w.get("base_need", 0) > 0:
                    parts.append(f"need={w['base_need']}")
                if w.get("recency_boost", 0) > 0:
                    parts.append(f"recency={w['recency_boost']}")
                if w.get("goal_alignment", 0) > 0:
                    parts.append(f"goals={w['goal_alignment']}")
                if w.get("roi_bonus", 0) > 0:
                    parts.append(f"roi={w['roi_bonus']}")
                if w.get("maturity_penalty", 0) > 0:
                    parts.append(f"maturity=-{w['maturity_penalty']}")
                if parts:
                    st.caption("  ".join(parts))
            if suggestion_cat:
                st.info(f"Next focus: **{suggestion_cat.replace('_', ' ').title()}**")
            if goal_signals:
                for gs in goal_signals[:3]:
                    aligned = ", ".join(gs.get("aligned_categories", []))
                    st.caption(f"🎯 \"{gs.get('goal', '')[:60]}\" → {aligned}")
        else:
            # Fallback: count-based bars (no weights file yet)
            for cat in all_cats:
                count = all_time.get(cat, 0)
                pct = count / total if total > 0 else 0
                label = cat.replace("_", " ").title()
                is_suggested = suggestion_cat and cat == suggestion_cat
                suffix = " 💡" if is_suggested else ""
                st.progress(pct, text=f"{label}{suffix}: {count}/{total}")
            if suggestion_cat:
                st.info(f"Next focus: **{suggestion_cat.replace('_', ' ')}**")

    st.divider()

    # ── Goal performance ──────────────────────────────────────
    st.subheader("Goal Performance")
    goal_stats = load_goal_stats()

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

    # Goal sparkline (reuse logic from overview)
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
            f'<div><small style="color:#888">Goal cycle durations (seconds) by cycle number</small></div>'
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
                gstatus = g.get("status", "")
                icon = status_icons.get(gstatus, "•")
                content = g.get("content") or g.get("goal") or ""
                created = (g.get("created_at") or "")[:10]
                st.markdown(f"{icon} **{content[:80]}** `{gstatus}` — {created}")

    st.divider()

    # ── Cycle velocity chart ───────────────────────────────────
    st.subheader("Cycle Velocity")
    cycles_data = load_cycles() or []
    completed_cycles = [
        c
        for c in cycles_data
        if c.get("cycle_status") == "completed"
        and c.get("start")
        and c.get("duration_seconds")
    ]

    if len(completed_cycles) >= 3:
        # Last 20 completed cycles
        recent_cycles = sorted(
            completed_cycles, key=lambda c: c.get("start", ""), reverse=True
        )[:20]
        recent_cycles = list(reversed(recent_cycles))  # chronological

        # Rolling 5-cycle avg duration
        durs = [c["duration_seconds"] for c in recent_cycles]
        cycle_nums = [c.get("cycle_number", "?") for c in recent_cycles]

        max_dur = max(durs) or 1
        n = len(durs)
        bar_w, gap, svg_h = 16, 3, 70
        svg_w = n * (bar_w + gap) + 20

        # Color by type
        type_colors = {
            "evolve": "#4CAF50",
            "goal": "#2196F3",
            "bootstrap": "#FF9800",
            "self-heal": "#F44336",
            "dream": "#3F51B5",
        }

        parts = [
            f'<svg width="{svg_w}" height="{svg_h + 20}" xmlns="http://www.w3.org/2000/svg">'
        ]
        for i, (dur, cn, c) in enumerate(zip(durs, cycle_nums, recent_cycles)):
            x = 10 + i * (bar_w + gap)
            bar_h = max(4, int(dur / max_dur * 50))
            y = svg_h - bar_h
            color = type_colors.get(c.get("cycle_type", ""), "#9E9E9E")
            dur_label = f"{dur}s" if dur < 60 else f"{dur//60}m"
            parts.append(
                f'<rect x="{x}" y="{y}" width="{bar_w}" height="{bar_h}" '
                f'fill="{color}" rx="2" opacity="0.8">'
                f'<title>Cycle {cn} ({c.get("cycle_type","?")}): {dur_label}</title></rect>'
            )
            parts.append(
                f'<text x="{x + bar_w//2}" y="{svg_h + 12}" text-anchor="middle" '
                f'font-size="8" fill="#888">{cn}</text>'
            )
        parts.append("</svg>")

        # Legend
        legend_parts = []
        for ctype, color in type_colors.items():
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
        avg_all = round(sum(durs) / len(durs))
        evolve_durs = [
            c["duration_seconds"]
            for c in recent_cycles
            if c.get("cycle_type") == "evolve"
        ]
        goal_durs = [
            c["duration_seconds"]
            for c in recent_cycles
            if c.get("cycle_type") == "goal"
        ]
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            st.metric("Avg Duration (all)", f"{avg_all}s")
        with sc2:
            if evolve_durs:
                st.metric("Avg Evolve", f"{round(sum(evolve_durs)/len(evolve_durs))}s")
        with sc3:
            if goal_durs:
                st.metric("Avg Goal", f"{round(sum(goal_durs)/len(goal_durs))}s")
    else:
        st.caption("Need 3+ completed cycles for velocity chart.")

    st.divider()

    # ── Recent Improvements changelog ─────────────────────────
    st.subheader("Recent Improvements")
    st.caption("What the agent has built and improved — latest first.")

    journal_data = load_journal(limit=50, offset=0)
    journal_entries = (
        journal_data.get("entries", []) if isinstance(journal_data, dict) else []
    )

    # Build a map from cycle number → journal entry for rich summaries
    journal_by_cycle: dict[int, dict] = {}
    for je in journal_entries:
        cn = je.get("cycle_number")
        if cn is not None:
            try:
                journal_by_cycle[int(cn)] = je
            except (ValueError, TypeError):
                pass

    # Collect all completed evolve + goal cycles, newest first
    all_cycles = load_cycles() or []
    improvement_cycles = [
        c
        for c in all_cycles
        if c.get("cycle_status") == "completed"
        and c.get("cycle_type") in ("evolve", "goal")
    ]
    improvement_cycles.sort(key=lambda c: c.get("start", ""), reverse=True)

    # Show selector for how many to display
    show_n = st.select_slider(
        "Show last N improvements",
        options=[5, 10, 20, 50],
        value=10,
        key="ov_improvements_n",
    )
    improvement_cycles = improvement_cycles[:show_n]

    if not improvement_cycles:
        st.info("No completed evolve or goal cycles yet.")
    else:
        for c in improvement_cycles:
            cycle_num = c.get("cycle_number", "?")
            ctype = c.get("cycle_type", "evolve")
            category = c.get("cycle_category", "")
            start = str(c.get("start", ""))[:16].replace("T", " ")
            dur = c.get("duration_seconds")

            # Prefer journal entry summary (richer) over cycles.json summary
            je = journal_by_cycle.get(
                int(cycle_num) if str(cycle_num).isdigit() else -1, {}
            )
            summary = je.get("summary") or je.get("outcome") or c.get("summary") or ""
            actions = je.get("actions") or c.get("actions") or []

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

            dur_str = ""
            if dur:
                dur_str = f"⏱ {dur}s  " if dur < 60 else f"⏱ {dur//60}m{dur%60:02d}s  "

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

"""Tab 4: System — health, errors, validation, run-script, scripts, cycle logs."""

import html
import json
import os
from datetime import datetime, timezone

import streamlit as st

_CATEGORY_ORDER = [
    "Cycle Management",
    "Memory",
    "Diagnostics",
    "Services",
    "Other",
]

_CATEGORY_ICONS = {
    "Cycle Management": "🔄",
    "Memory": "🗄️",
    "Diagnostics": "🩺",
    "Services": "⚙️",
    "Other": "📦",
}

# ── Script argument definitions for Run Script UI ─────────
# "type": "select" → selectbox for mutually exclusive actions/subcommands
# dict with "flag" only → checkbox (boolean flag)
# dict with "flag" + "placeholder" → text input (value argument)
_SCRIPT_ARGS: dict[str, list] = {
    # Cycle Management
    "cycle_start.py": [
        {"flag": "--short", "help": "One-liner summary only"},
        {"flag": "--json", "help": "Machine-readable JSON output"},
        {"flag": "--no-repair", "help": "Skip memory repair step"},
        {"flag": "--clear-old-errors", "help": "Purge tab errors older than 1h"},
        {"flag": "--clear-all-errors", "help": "Purge ALL tab errors"},
    ],
    "cycle_close.py": [
        {
            "flag": "--type",
            "help": "evolve | goal | self-heal | dream",
            "placeholder": "evolve",
        },
        {
            "flag": "--summary",
            "help": "1-2 sentence summary (required)",
            "placeholder": "text",
        },
        {
            "flag": "--cycle",
            "help": "Cycle number (auto-detect if omitted)",
            "placeholder": "N",
        },
        {
            "flag": "--category",
            "help": "evolve: reliability | observability | capability | efficiency | prompt_evolution; dream: memory_consolidation | deep_sleep",
            "placeholder": "capability",
        },
        {
            "flag": "--status",
            "help": "completed (default) | failed",
            "placeholder": "completed",
        },
        {"flag": "--no-normalize", "help": "Skip cycles.json normalization step"},
        {"flag": "--dry-run", "help": "Preview what would be written"},
    ],
    "cycle_report.py": [
        {"flag": "--last", "help": "Show last N cycles only", "placeholder": "10"},
        {"flag": "--format", "help": "Output format: md or json", "placeholder": "md"},
    ],
    # Memory
    "repair_memory_files.py": [
        {"flag": "--dry-run", "help": "Scan only, no writes"},
        {"flag": "--backup", "help": "Create backups only (no repair)"},
        {"flag": "--quiet", "help": "Only print summary line"},
        {"flag": "--json", "help": "Machine-readable JSON output"},
    ],
    "journal_archive.py": [
        {"flag": "--dry-run", "help": "Preview without modifying files"},
        {"flag": "--list", "help": "Show entry counts and file sizes"},
        {"flag": "--json", "help": "Output as JSON"},
        {"flag": "--keep", "help": "Keep N most recent entries", "placeholder": "20"},
        {
            "flag": "--search",
            "help": "Search across journal by keyword",
            "placeholder": "keyword",
        },
    ],
    "memory_ask.py": [
        {"flag": "--k", "help": "Max retrieval results", "placeholder": "20"},
        {
            "flag": "--min-score",
            "help": "Drop hits below this fraction of the top score (0-1)",
            "placeholder": "0.5",
        },
        {
            "flag": "--context-only",
            "help": "Show retrieved context without Claude synthesis",
        },
        {"flag": "--json", "help": "Output as JSON"},
    ],
    "memory_recall.py": [
        {"flag": "--k", "help": "Number of results to return", "placeholder": "5"},
        {
            "flag": "--timeline",
            "help": "Show timeline entries instead of semantic search",
        },
        {
            "flag": "--since",
            "help": "Filter entries since date (ISO format)",
            "placeholder": "2024-01-01",
        },
        {
            "flag": "--until",
            "help": "Filter entries until date (ISO format)",
            "placeholder": "2024-12-31",
        },
        {"flag": "--json", "help": "Output as JSON"},
    ],
    "memory_ingest.py": [
        {
            "type": "select",
            "label": "Mode",
            "options": [
                ("--build", "Rebuild the LanceDB index from scratch"),
                ("--append-json", "Append a single entry (JSON string or @file.json)"),
                ("--append-text", "Ingest raw text directly"),
                ("--append-file", "Ingest a text file (TXT, MD, JSON, etc.)"),
            ],
        },
        {
            "flag": "--title",
            "help": "Title for appended entry",
            "placeholder": "My note",
        },
        {"flag": "--dry-run", "help": "Preview without writing"},
        {"flag": "--json", "help": "Output as JSON"},
        {"flag": "--quiet", "help": "Suppress progress output"},
    ],
    "sync_memory_files.py": [
        {"flag": "--dry-run", "help": "Preview only, do not write .md files"},
        {
            "flag": "--only",
            "help": "Comma-separated sync targets",
            "placeholder": "capabilities,services,state",
        },
    ],
    # Diagnostics
    "self_test.py": [
        {"flag": "--json", "help": "Output results as JSON"},
        {"flag": "--record", "help": "Record failures to journal.json"},
        {"flag": "--fail-fast", "help": "Stop on first failure"},
        {"flag": "--quiet", "help": "Only print summary"},
        {"flag": "--list-suites", "help": "List available test suites"},
        {
            "flag": "--suite",
            "help": "Run specific test suite only",
            "placeholder": "suite_name",
        },
        {
            "flag": "--cycle",
            "help": "Cycle number for failure recording",
            "placeholder": "N",
        },
    ],
    "maintain.py": [
        {"flag": "--fix", "help": "Run checks and apply fixes (default: report only)"},
        {"flag": "--json", "help": "Output results as JSON"},
        {
            "flag": "--check",
            "help": "Run specific check only",
            "placeholder": "check_name",
        },
    ],
    "metrics_collector.py": [
        {"flag": "--report", "help": "Print last snapshots as table"},
        {"flag": "--json", "help": "Print last snapshot as JSON"},
        {"flag": "--summary", "help": "One-line summary (averages)"},
        {"flag": "--regressions", "help": "Detect performance regressions"},
        {"flag": "--limit", "help": "Report on last N snapshots", "placeholder": "10"},
    ],
    "app_check.py": [
        {"flag": "--json", "help": "Output result as JSON"},
    ],
    # Services / Portal
    "service_manager.py": [
        {
            "type": "select",
            "label": "Command",
            "options": [
                ("list", "Show all managed services"),
                ("health", "Health check all services"),
                ("cleanup", "Remove dead entries"),
                ("start", "Start a service (add: name port -- cmd...)"),
                ("stop", "Stop a service (add: name)"),
                ("status", "Check one service (add: name)"),
            ],
        },
    ],
    "portal_config.py": [
        {
            "type": "select",
            "label": "Subcommand",
            "options": [
                ("hostname", "Manage public hostname"),
                ("timezone", "Manage timezone"),
                ("auth", "Manage authentication"),
            ],
        },
        {
            "flag": "--set",
            "help": "Set value (hostname URL, timezone, or auth user:pass)",
            "placeholder": "value",
        },
        {"flag": "--show", "help": "Show current value"},
        {"flag": "--clear", "help": "Clear value (hostname/timezone only)"},
        {
            "flag": "--enable",
            "help": "Enable auth (auth subcommand only)",
            "placeholder": "user:pass",
        },
        {"flag": "--disable", "help": "Disable auth (auth subcommand only)"},
        {
            "flag": "--reapply",
            "help": "Re-apply auth from saved creds (auth subcommand only)",
        },
        {
            "flag": "--rollback",
            "help": "Force-remove auth route (auth subcommand only)",
        },
    ],
    "scheduler.py": [
        {
            "type": "select",
            "label": "Action",
            "options": [
                ("--check", "Evaluate and inject due tasks"),
                ("--list", "List all scheduled tasks"),
            ],
        },
    ],
    "keepass.py": [
        {
            "type": "select",
            "label": "Command",
            "options": [
                ("init", "Create KeePass database"),
                ("list", "List all entries"),
                ("get", "Get entry (add: title)"),
                (
                    "store",
                    "Store credential (add: --title T --username U --password P)",
                ),
                ("delete", "Delete entry (add: title)"),
                ("groups", "List all groups"),
                ("search", "Search entries (add: query)"),
            ],
        },
        {"flag": "--json", "help": "Output as JSON"},
    ],
    "milestone_report.py": [
        {"flag": "--list", "help": "List all milestone cycles"},
        {"flag": "--save", "help": "Write report to workspace/"},
        {"flag": "--json", "help": "Output raw JSON"},
        {"flag": "--cycle", "help": "Target cycle number", "placeholder": "N"},
    ],
    # Notes / Reminders / Emails
    "notes.py": [
        {
            "type": "select",
            "label": "Subcommand",
            "options": [
                ("add", "Add a new note"),
                ("list", "List notes"),
                ("get", "Get a note by ID"),
                ("search", "Search notes"),
                ("edit", "Edit a note"),
                ("delete", "Delete a note"),
                ("tags", "List all tags"),
                ("export", "Export notes"),
                ("stats", "Show statistics"),
            ],
        },
        {"flag": "--id", "help": "Note ID", "placeholder": "abc123"},
        {"flag": "--title", "help": "Note title", "placeholder": "My note"},
        {"flag": "--content", "help": "Note body", "placeholder": "text"},
        {"flag": "--tags", "help": "Comma-separated tags", "placeholder": "tag1,tag2"},
        {"flag": "--query", "help": "Search query", "placeholder": "keyword"},
        {"flag": "--format", "help": "Export format: md or json", "placeholder": "md"},
        {"flag": "--pin", "help": "Pin the note"},
        {"flag": "--unpin", "help": "Unpin the note"},
        {"flag": "--json", "help": "Output as JSON"},
    ],
    "reminder.py": [
        {
            "type": "select",
            "label": "Subcommand",
            "options": [
                ("add", "Add a reminder"),
                ("list", "List reminders"),
                ("delete", "Delete a reminder"),
                ("clear", "Remove all fired/disabled reminders"),
            ],
        },
        {"flag": "--text", "help": "Reminder message", "placeholder": "text"},
        {
            "flag": "--at",
            "help": "Fire once at this time (ISO 8601)",
            "placeholder": "2024-06-01T09:00:00",
        },
        {"flag": "--every", "help": "Fire every N minutes", "placeholder": "60"},
        {"flag": "--cron", "help": "Cron schedule pattern", "placeholder": "0 9 * * 1"},
        {"flag": "--priority", "help": "Priority 1-5 (1=highest)", "placeholder": "1"},
        {"flag": "--id", "help": "Reminder ID", "placeholder": "abc123"},
        {"flag": "--json", "help": "Output as JSON"},
    ],
    "email_imap.py": [
        {
            "type": "select",
            "label": "Subcommand",
            "options": [
                ("auth", "Verify IMAP credentials"),
                ("fetch", "Fetch emails"),
                ("search", "Search emails"),
                ("delete", "Delete emails by UID"),
            ],
        },
        {"flag": "--mailbox", "help": "IMAP mailbox", "placeholder": "INBOX"},
        {
            "flag": "--filter",
            "help": "Email filter: unseen, seen, or all",
            "placeholder": "all",
        },
        {"flag": "--max", "help": "Max emails to fetch", "placeholder": "20"},
        {"flag": "--query", "help": "Search query text", "placeholder": "keyword"},
        {
            "flag": "--field",
            "help": "Field to search: subject, from, or text",
            "placeholder": "text",
        },
        {
            "flag": "--uid",
            "help": "One or more IMAP UIDs to delete (space-separated)",
            "placeholder": "123 456",
        },
        {"flag": "--json", "help": "Output as JSON"},
    ],
}


_MEMORY_FILE_LABELS = {
    "state.json": ("Agent State", "Core agent status, cycle number, last heartbeat"),
    "goal.json": ("Goals", "Pending / completed goals from user"),
    "journal.json": ("Journal (active)", "Recent cycle journal entries"),
    "journal_archive.json": ("Journal (archive)", "Archived older journal entries"),
    "cycles.json": ("Cycles", "Complete cycle history with durations"),
    "outbox.json": ("Outbox", "Pending messages for the user"),
    "server_errors.json": ("Tab Errors", "Portal tab crash errors"),
    "bootstrap.json": ("Bootstrap Config", "First-cycle initialization data (stable)"),
    "link_cache.json": ("Link Cache", "URL health check cache (link-checker.py)"),
}

# Size/age thresholds and the age-exempt list now live in scripts/metrics_db.py,
# which grades every memory file at collection time (MEMORY_SIZE_WARN_KB etc.).


def _format_tokens(n) -> str:
    """Compact token count: 812, 44.1K, 4.63M."""
    n = n or 0
    if n < 1000:
        return str(int(n))
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.2f}M"


def _render_collector_health():
    """Collector freshness and per-handler state.

    Without this, a handler that fails to import, is rejected for a table
    clash, or raises is invisible: its tables are simply empty, and a panel
    renders "0" as a fact.
    """
    from app.data.metrics import built_at, load_handler_status
    from app.shared import parse_dt

    stamp = built_at()
    handlers = load_handler_status()
    if not stamp and not handlers:
        st.warning(
            "Metrics store unavailable — run `uv run python scripts/metrics_db.py "
            "--rebuild`, or check that the `metrics_daemon` service is running."
        )
        return

    failed = [h for h in handlers if not h.get("ok")]
    for handler in failed:
        st.warning(
            f"Metrics handler `{handler.get('name')}` failed: {handler.get('error')}"
        )

    if stamp:
        collected = ", ".join(
            f"{h['name']} ({h.get('state') or '?'})" for h in handlers
        )
        age = ""
        parsed = parse_dt(stamp)
        if parsed is not None:
            minutes = int((datetime.now(timezone.utc) - parsed).total_seconds() // 60)
            # The daemon polls every 5 minutes; well past that means it is down.
            age = f" ({minutes}m ago)" if minutes >= 0 else ""
            if minutes > 30:
                st.warning(
                    f"Metrics store last built {minutes}m ago — the "
                    "`metrics_daemon` service may not be running."
                )
        st.caption(
            f"Metrics collected {stamp[:16].replace('T', ' ')} UTC{age}"
            + (f" · handlers: {collected}" if collected else " · no handlers")
        )


def _render_token_usage():
    """Token usage parsed from cycle transcripts by services/metrics/usage.py."""
    from app.data.metrics import (
        load_handler_status,
        load_usage_daily,
        load_usage_totals,
    )

    st.subheader("Token Usage")

    # Only this panel's handler; every other handler is covered by the
    # collector-health strip above.
    for handler in load_handler_status():
        if handler.get("name") == "usage" and not handler.get("ok"):
            st.warning(f"Usage metrics handler failed: {handler.get('error')}")

    totals = load_usage_totals()
    if not totals["available"] or not totals["transcripts"]:
        st.caption(
            "No usage data yet — the `usage` handler collects it from "
            "`/agent/memory/transcripts/` on the next metrics build."
        )
        return

    if totals.get("pending"):
        st.info(
            f"Backfilling — {totals['pending']} transcript(s) still to read. "
            "The numbers below cover what has been processed so far and will "
            "fill in over the next few collector runs."
        )

    models = ", ".join(html.escape(m) for m in totals["models"])
    collected = totals.get("collected_at") or ""
    st.caption(
        f"Deduplicated API responses across the last {totals['transcripts']} cycle "
        f"transcript(s){' · ' + models if models else ''}"
        # The handler collects on its own interval, not the daemon's, so say
        # when these numbers were last measured.
        + (f" · collected {collected[:16].replace('T', ' ')} UTC" if collected else "")
    )

    u1, u2, u3, u4 = st.columns(4)
    with u1:
        st.metric("Total Tokens", _format_tokens(totals["total_tokens"]))
        st.caption(f"{totals['requests']} requests")
    with u2:
        st.metric("Output", _format_tokens(totals["output_tokens"]))
    with u3:
        st.metric("Cache Read", _format_tokens(totals["cache_read_input_tokens"]))
        st.caption("billed at a discount")
    with u4:
        st.metric("Cache Write", _format_tokens(totals["cache_creation_input_tokens"]))
        st.caption(f"input {_format_tokens(totals['input_tokens'])}")

    daily = load_usage_daily(limit=14)
    if not daily:
        return

    # Per-day bars, newest last so the chart reads left-to-right in time.
    rows = list(reversed(daily))
    max_total = max((r["total_tokens"] or 0) for r in rows) or 1
    bar_w, gap, svg_h = 22, 4, 60
    svg_w = len(rows) * (bar_w + gap) + 20
    parts = [
        f'<svg width="{svg_w}" height="{svg_h + 18}" xmlns="http://www.w3.org/2000/svg">'
    ]
    for i, r in enumerate(rows):
        total = r["total_tokens"] or 0
        x = 10 + i * (bar_w + gap)
        bar_h = max(3, int(total / max_total * 46))
        y = svg_h - bar_h
        # `day` comes from a transcript timestamp — escape before it goes into
        # markup rendered with unsafe_allow_html.
        day = html.escape(str(r["day"] or ""))
        parts.append(
            f'<rect x="{x}" y="{y}" width="{bar_w}" height="{bar_h}" '
            f'fill="#9C27B0" rx="2" opacity="0.8">'
            f"<title>{day}: {_format_tokens(total)} tokens over "
            f'{int(r["requests"] or 0)} request(s) in '
            f'{int(r["cycles"] or 0)} cycle(s)</title></rect>'
        )
        parts.append(
            f'<text x="{x + bar_w // 2}" y="{svg_h + 12}" text-anchor="middle" '
            f'font-size="8" fill="#888">{day[5:]}</text>'
        )
    parts.append("</svg>")
    st.markdown(
        f'<div style="overflow-x:auto;padding:4px 0">{"".join(parts)}</div>'
        f'<div><small style="color:#888">Total tokens per day — '
        f"last {len(rows)} day(s) with activity</small></div>",
        unsafe_allow_html=True,
    )


def _format_age(age_hours: float) -> str:
    """Human-readable 'time since last write' for a memory file."""
    if age_hours < 1:
        return f"{int(age_hours * 60)}m ago"
    if age_hours < 24:
        return f"{age_hours:.1f}h ago"
    return f"{age_hours / 24:.1f}d ago"


def _render_memory_files_health():
    """Render a compact health table for all JSON files in /agent/memory/.

    Sizes, ages, entry counts, and the ok/warn/crit classification are all
    pre-computed by scripts/metrics_db.py — deriving them here meant an
    os.listdir + os.stat + full json.load for every file on every render.
    """
    from app.data.metrics import load_memory_file_health

    health_data = load_memory_file_health()
    rows = health_data["rows"]

    if not health_data["available"]:
        st.warning(
            "Metrics store unavailable — memory-file health cannot be shown. "
            "Run `uv run python scripts/metrics_db.py --rebuild`, or check that "
            "the `metrics_daemon` service is running."
        )
        return

    if not rows:
        st.caption("No JSON files found in /agent/memory/.")
        return

    # Presentation-only enrichment: labels, descriptions, and formatted age.
    for r in rows:
        label, desc = _MEMORY_FILE_LABELS.get(
            r["fname"], (r["fname"].replace(".json", ""), "")
        )
        r["label"] = label
        r["desc"] = desc
        r["age_str"] = _format_age(r["age_hours"])
        count = r.get("entry_count")
        r["entry_count"] = (
            f"{count} {r.get('entry_kind') or 'entries'}"
            if count is not None
            else (r.get("entry_kind") or "?")
        )

    total_kb = health_data["total_kb"]
    warn_count = health_data["warn"]
    crit_count = health_data["crit"]
    ok_count = health_data["ok"]

    # Summary strip
    ms1, ms2, ms3, ms4 = st.columns(4)
    with ms1:
        st.metric("Memory Files", len(rows))
    with ms2:
        st.metric("Total Size", f"{total_kb:.0f} KB")
    with ms3:
        color = "🟢" if ok_count == len(rows) else ("🔴" if crit_count else "🟡")
        st.metric("Healthy", f"{color} {ok_count}/{len(rows)}")
    with ms4:
        issues = warn_count + crit_count
        if issues:
            st.metric("Issues", f"⚠️ {warn_count} warn · 🔴 {crit_count} crit")
        else:
            st.metric("Issues", "None ✓")

    # Sizes and ages are frozen at collection time — surface when that was, so a
    # stopped collector is visible rather than silently showing stale ages.
    if health_data.get("built_at"):
        st.caption(f"Collected {health_data['built_at'][:16].replace('T', ' ')} UTC")

    # File table using HTML for compact display. The collector grades size and
    # age independently, so the thresholds live in one place only.
    health_icons = {"ok": "🟢", "warn": "🟡", "crit": "🔴"}
    health_colors = {"ok": "#888", "warn": "#ff9800", "crit": "#f44336"}

    rows_html = []
    for r in rows:
        icon = health_icons.get(r["health"], "🟢")
        size_str = f"{r['size_kb']:.1f} KB"
        size_color = health_colors.get(r["size_health"], "#888")
        age_color = health_colors.get(r["age_health"], "#888")
        rows_html.append(
            f"<tr>"
            f'<td style="padding:3px 8px;font-size:12px">{icon}</td>'
            f'<td style="padding:3px 8px;font-size:12px;font-weight:600;white-space:nowrap">'
            f'<code style="background:#1a1a1a;padding:1px 4px;border-radius:3px">{r["fname"]}</code>'
            f"</td>"
            f'<td style="padding:3px 8px;font-size:11px;color:#aaa">{r["label"]}</td>'
            f'<td style="padding:3px 8px;font-size:11px;color:{size_color};text-align:right">{size_str}</td>'
            f'<td style="padding:3px 8px;font-size:11px;color:#888;text-align:right">{r["entry_count"]}</td>'
            f'<td style="padding:3px 8px;font-size:11px;color:{age_color};text-align:right;white-space:nowrap">{r["age_str"]}</td>'
            f"</tr>"
        )

    st.markdown(
        f'<div style="overflow-x:auto">'
        f'<table style="width:100%;border-collapse:collapse;border:1px solid #333;border-radius:6px">'
        f'<thead><tr style="background:#1a1a1a">'
        f'<th style="padding:4px 8px;font-size:11px;color:#666;text-align:left">OK</th>'
        f'<th style="padding:4px 8px;font-size:11px;color:#666;text-align:left">File</th>'
        f'<th style="padding:4px 8px;font-size:11px;color:#666;text-align:left">Description</th>'
        f'<th style="padding:4px 8px;font-size:11px;color:#666;text-align:right">Size</th>'
        f'<th style="padding:4px 8px;font-size:11px;color:#666;text-align:right">Entries</th>'
        f'<th style="padding:4px 8px;font-size:11px;color:#666;text-align:right">Updated</th>'
        f"</tr></thead>"
        f'<tbody>{"".join(rows_html)}</tbody>'
        f"</table>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Show any warnings/critical items highlighted
    issues = [r for r in rows if r["health"] != "ok"]
    if issues:
        st.markdown("")
        for r in issues:
            icon = health_icons.get(r["health"], "🟡")
            reasons = []
            if r["size_health"] != "ok":
                reasons.append(f"large ({r['size_kb']:.0f} KB)")
            if r["age_health"] != "ok":
                reasons.append(f"stale ({r['age_str']})")
            msg = f"{icon} **{r['fname']}** — {', '.join(reasons)}."
            if r["desc"]:
                msg += f" {r['desc']}."
            st.warning(msg)


def render():
    from app.data import (
        load_system_info,
        load_errors,
        load_validate,
        load_scripts,
        run_script,
        load_cycle_logs,
    )
    from app.data.metrics import load_workspace_mb
    from app.shared import ERROR_LOG_PATH, _write_json_atomic

    # ── System health ─────────────────────────────────────────
    st.subheader("System Health")
    info = load_system_info() or {}

    col1, col2, col3 = st.columns(3)

    with col1:
        mem = info.get("memory")
        if mem:
            pct = mem.get("percent", 0)
            st.metric(
                "Memory", f"{mem.get('used_mb', 0)} MB / {mem.get('total_mb', 0)} MB"
            )
            st.progress(min(pct / 100, 1.0), text=f"{pct}% used")
        else:
            st.caption("Memory: N/A")

    with col2:
        disk = info.get("disk")
        if disk:
            pct = disk.get("percent", 0)
            st.metric(
                "Disk", f"{disk.get('used_gb', 0)} GB / {disk.get('total_gb', 0)} GB"
            )
            st.progress(min(pct / 100, 1.0), text=f"{pct}% used")
        else:
            st.caption("Disk: N/A")

    with col3:
        load_avg = info.get("load")
        uptime = info.get("uptime")
        if load_avg:
            st.metric("Load Avg (1m)", load_avg.get("1m", 0))
            st.caption(f"5m: {load_avg.get('5m')} | 15m: {load_avg.get('15m')}")
        if uptime:
            st.metric("Uptime", uptime.get("human", "—"))
        # Pre-computed: deriving this walked every file under /agent/workspace
        # (a directory that grows toward its 1 GB limit) on the render path.
        ws_mb = load_workspace_mb()
        if ws_mb is not None:
            st.metric("Workspace", f"{ws_mb} MB")

    st.divider()

    # ── Metrics collector health ──────────────────────────────
    _render_collector_health()

    # ── Token usage ───────────────────────────────────────────
    _render_token_usage()

    st.divider()

    # ── Run script ────────────────────────────────────────────
    with st.expander("Run Script"):
        scripts = load_scripts() or []
        if not scripts:
            st.caption("No scripts found in /agent/scripts/")
        else:
            script_names = [s["name"] for s in scripts]
            selected_script = st.selectbox(
                "Script", script_names, key="run_script_select"
            )

            # Show script description
            script_info = next(
                (s for s in scripts if s["name"] == selected_script), None
            )
            if script_info and script_info.get("description"):
                st.caption(script_info["description"])

            # Build arguments from definitions
            args_def = _SCRIPT_ARGS.get(selected_script, [])
            built_args = []

            # Separate arg types for layout
            selects = [a for a in args_def if a.get("type") == "select"]
            checks = [a for a in args_def if "flag" in a and "placeholder" not in a]
            texts = [a for a in args_def if "flag" in a and "placeholder" in a]

            # Render selectboxes (mutually exclusive actions/subcommands)
            for arg in selects:
                opts = arg["options"]
                opt_vals = [v for v, _ in opts]
                opt_desc = {v: d for v, d in opts}
                sel = st.selectbox(
                    arg["label"],
                    opt_vals,
                    format_func=lambda x, d=opt_desc: f"{x}  \u2014  {d[x]}",
                    key=f"arg_{selected_script}_select_{arg['label']}",
                )
                if sel:
                    built_args.append(sel)

            # Render checkboxes in rows of 3
            if checks:
                for i in range(0, len(checks), 3):
                    cols = st.columns(3)
                    for j, col in enumerate(cols):
                        idx = i + j
                        if idx < len(checks):
                            a = checks[idx]
                            with col:
                                if st.checkbox(
                                    a["flag"],
                                    help=a["help"],
                                    key=f"arg_{selected_script}_{a['flag']}",
                                ):
                                    built_args.append(a["flag"])

            # Render text inputs (value arguments)
            for a in texts:
                val = st.text_input(
                    a["flag"],
                    help=a["help"],
                    placeholder=a.get("placeholder", ""),
                    key=f"arg_{selected_script}_{a['flag']}",
                )
                if val.strip():
                    built_args.extend([a["flag"], val.strip()])

            # Extra custom arguments (always available as fallback)
            extra = st.text_input(
                "Additional arguments",
                placeholder="Extra arguments (space-separated)...",
                key=f"arg_{selected_script}_extra",
            )
            if extra.strip():
                built_args.extend(extra.split())

            if st.button("Run Script"):
                with st.spinner(f"Running {selected_script}..."):
                    result = run_script(selected_script, built_args)
                if result.get("ok"):
                    exit_code = result.get("exit_code", 0)
                    if exit_code == 0:
                        st.success("Script completed (exit code 0)")
                    else:
                        st.warning(f"Script exited with code {exit_code}")
                    if result.get("stdout"):
                        st.code(result["stdout"], language="text")
                    if result.get("stderr"):
                        st.code(result["stderr"], language="text")
                else:
                    st.error(f"Error: {result.get('error')}")

    st.divider()

    # ── Server errors ─────────────────────────────────────────
    st.subheader("Tab Crash Errors")
    errors = load_errors() or []

    if not errors:
        st.success("No tab errors logged.")
    else:
        # Separate resolved (old) from recent errors (last 24h)
        from datetime import timedelta

        now_utc = datetime.now(timezone.utc)
        recent, older = [], []
        for err in errors:
            try:
                ts_str = err.get("timestamp", "")
                err_ts = datetime.fromisoformat(ts_str) if ts_str else None
                if err_ts and (now_utc - err_ts) < timedelta(hours=24):
                    recent.append(err)
                else:
                    older.append(err)
            except Exception:
                older.append(err)

        if recent:
            st.error(f"{len(recent)} error(s) in the last 24 hours")
        if older:
            st.warning(f"{len(older)} older error(s) on record")

        col_dl, col_clr, _ = st.columns([1, 1, 2])
        with col_dl:
            st.download_button(
                "Download all errors (JSON)",
                data=json.dumps(errors, indent=2, default=str),
                file_name="server_errors.json",
                mime="application/json",
            )
        with col_clr:
            if st.button("Clear All Errors", type="primary"):
                _write_json_atomic(ERROR_LOG_PATH, [])
                st.success("All tab crash errors cleared.")
                st.rerun()

        for err in reversed(errors[-20:]):
            ts = str(err.get("timestamp", ""))[:19].replace("T", " ")
            # Tab crash errors have "tab" field; legacy HTTP errors have "method"/"path"
            tab_name = err.get("tab")
            method = err.get("method")
            path = err.get("path")
            error_msg = str(err.get("error", ""))

            if tab_name:
                label = f"[{ts}] Tab: {tab_name} — {error_msg[:60]}"
            else:
                label = f"[{ts}] {method or '?'} {path or '?'} — {error_msg[:60]}"

            with st.expander(label):
                if tab_name:
                    st.caption(f"**Tab:** {tab_name}")
                st.caption(f"**Time:** {ts}")
                st.caption(f"**Error:** {error_msg}")
                tb = err.get("traceback", "")
                if tb:
                    st.code(tb, language="python")

    st.divider()

    # ── Memory validation ─────────────────────────────────────
    st.subheader("Memory Validation")

    if st.button("Run Validation"):
        load_validate.clear()
        st.rerun()

    validate = load_validate() or {}
    summary = validate.get("summary", {})
    checks = validate.get("checks", [])
    healthy = validate.get("healthy", False)

    if summary:
        col_ok, col_warn, col_err = st.columns(3)
        with col_ok:
            st.metric("OK", summary.get("ok", 0))
        with col_warn:
            st.metric("Warnings", summary.get("warnings", 0))
        with col_err:
            st.metric("Errors", summary.get("errors", 0))

        if healthy:
            st.success("All checks passed")
        else:
            st.error("Some checks failed")

        for chk in checks:
            passed = chk.get("passed", False)
            sev = chk.get("severity", "ok")
            name = chk.get("name", "")
            detail = chk.get("detail", "")
            if passed:
                st.markdown(f"✅ {name}" + (f" — {detail}" if detail else ""))
            elif sev == "warning":
                st.markdown(f"⚠️ {name}" + (f" — {detail}" if detail else ""))
            else:
                st.markdown(f"❌ {name}" + (f" — {detail}" if detail else ""))

    st.divider()

    # ── Memory Files Health ────────────────────────────────────
    st.subheader("Memory Files Health")
    st.caption("All JSON memory files — size, entry count, and freshness at a glance.")

    _render_memory_files_health()

    st.divider()

    # ── Script Inventory ──────────────────────────────────────
    st.subheader("Script Inventory")
    scripts = load_scripts() or []

    if not scripts:
        st.caption("No scripts found in /agent/scripts/")
    else:
        # Group by category
        by_category: dict[str, list] = {}
        for s in scripts:
            cat = s.get("category", "Other")
            by_category.setdefault(cat, []).append(s)

        total = len(scripts)
        st.caption(f"{total} scripts across {len(by_category)} categories")

        for cat in _CATEGORY_ORDER:
            cat_scripts = by_category.get(cat)
            if not cat_scripts:
                continue
            icon = _CATEGORY_ICONS.get(cat, "📦")
            with st.expander(f"{icon} {cat} ({len(cat_scripts)})", expanded=False):
                for s in cat_scripts:
                    name = s["name"]
                    desc = s.get("description") or "—"
                    size_kb = round(s["size"] / 1024, 1)
                    mod_raw = s.get("modified", "")
                    mod_str = mod_raw[:10] if mod_raw else "—"
                    col_name, col_desc, col_meta = st.columns([2, 5, 2])
                    with col_name:
                        st.markdown(f"**`{name}`**")
                    with col_desc:
                        st.caption(desc)
                    with col_meta:
                        st.caption(f"{size_kb} KB · {mod_str}")

    st.divider()

    # ── Cycle Logs Viewer ─────────────────────────────────────
    st.subheader("Cycle Logs")
    st.caption("Browse detailed output logs from each agent cycle.")

    cycle_logs = load_cycle_logs()

    if not cycle_logs:
        st.caption("No cycle logs found in /agent/memory/logs/")
    else:
        total_logs = len(cycle_logs)
        total_kb = sum(c["size"] for c in cycle_logs) / 1024
        st.caption(f"{total_logs} cycle logs · {total_kb:.0f} KB total")

        # Selector: choose a cycle to view
        cycle_options = [
            f"Cycle {c['cycle']} ({c['size'] // 1024 or 1} KB)" for c in cycle_logs
        ]
        selected_idx = st.selectbox(
            "Select cycle to view",
            range(len(cycle_options)),
            format_func=lambda i: cycle_options[i],
            key="sys_cycle_log_select",
        )

        selected = cycle_logs[selected_idx]
        log_path = selected["path"]

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                log_content = f.read(20000)  # cap at 20KB
            if os.path.getsize(log_path) > 20000:
                log_content += "\n\n... (truncated — file exceeds 20KB)"
        except OSError as e:
            log_content = f"(Could not read log: {e})"

        st.download_button(
            f"⬇ Download cycle-{selected['cycle']}.log",
            data=log_content,
            file_name=f"cycle-{selected['cycle']}.log",
            mime="text/plain",
            key=f"dl_cycle_log_{selected['cycle']}",
        )
        st.code(log_content, language="markdown")

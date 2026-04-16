"""Tab 4: System — health, errors, validation, run-script, scripts, cycle logs."""

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
            "help": "evolve | goal | self-heal",
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
            "help": "For evolve: reliability | observability | capability | efficiency",
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
    "memory_stats.py": [
        {"flag": "--json", "help": "Machine-readable JSON output"},
        {"flag": "--short", "help": "One-liner summary only"},
    ],
    "memory_repair.py": [
        {"flag": "--dry-run", "help": "Scan only, no writes"},
        {"flag": "--backup", "help": "Create backups only (no repair)"},
        {"flag": "--quiet", "help": "Only print summary line"},
        {"flag": "--json", "help": "Machine-readable JSON output"},
    ],
    "memory_backup.py": [
        {"flag": "--list", "help": "List all backups"},
        {"flag": "--check", "help": "Show age of most recent backup"},
        {"flag": "--dry-run", "help": "Preview what restore would do"},
        {"flag": "--json", "help": "Output as JSON"},
        {"flag": "--quiet", "help": "Suppress output"},
        {
            "flag": "--restore",
            "help": "Restore a backup (timestamp or 'latest')",
            "placeholder": "latest",
        },
        {
            "flag": "--prune",
            "help": "Keep only N most recent backups",
            "placeholder": "5",
        },
        {
            "flag": "--label",
            "help": "Label for the backup",
            "placeholder": "pre-deploy",
        },
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
                ("--build", "Rebuild .mv2 index from scratch"),
                ("--append-json", "Append a single entry (JSON string or @file.json)"),
                ("--append-text", "Ingest raw text directly"),
                ("--append-file", "Ingest a file (PDF, DOCX, TXT, MD, etc.)"),
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
    "memory_sync.py": [
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
    "journal-archive.json": ("Journal (archive)", "Archived older journal entries"),
    "cycles.json": ("Cycles", "Complete cycle history with durations"),
    "outbox.json": ("Outbox", "Pending messages for the user"),
    "outbox_history.json": ("Outbox History", "All past agent→user messages"),
    "server_errors.json": ("Tab Errors", "Portal tab crash errors"),
    "bootstrap.json": ("Bootstrap Config", "First-cycle initialization data (stable)"),
    "command_history.json": ("Command History", "Agent Console command history"),
    "link_cache.json": ("Link Cache", "URL health check cache (link-checker.py)"),
}

_MEMORY_SIZE_WARN_KB = 500  # warn if file exceeds this
_MEMORY_SIZE_CRIT_KB = 2000  # critical if file exceeds this
_MEMORY_AGE_WARN_HOURS = 24  # warn if file not updated in this many hours
_MEMORY_AGE_CRIT_HOURS = 72  # critical if not updated in this many hours

# Files that are intentionally infrequently updated — skip age checks for these
_MEMORY_AGE_EXEMPT = {
    "bootstrap.json",
    "link_cache.json",
    "command_history.json",
    "outbox_history.json",
}


def _count_json_entries(path: str) -> str:
    """Return a human-readable entry count for a JSON file (list len or dict key count)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, list):
            return f"{len(data)} entries"
        if isinstance(data, dict):
            # For state.json and similar, key count isn't useful; skip
            if len(data) <= 5:
                return f"{len(data)} keys"
            return f"{len(data)} keys"
    except Exception:
        return "parse err"
    return "?"


def _render_memory_files_health():
    """Render a compact health table for all JSON files in /agent/memory/."""
    memory_dir = "/agent/memory"
    now_utc = datetime.now(timezone.utc)

    try:
        all_files = sorted(
            f
            for f in os.listdir(memory_dir)
            if f.endswith(".json") and not f.endswith(".backup")
        )
    except OSError:
        st.error("Could not read /agent/memory/ directory.")
        return

    if not all_files:
        st.caption("No JSON files found in /agent/memory/.")
        return

    # Collect stats
    rows = []
    total_kb = 0.0
    warn_count = 0
    crit_count = 0

    for fname in all_files:
        fpath = os.path.join(memory_dir, fname)
        try:
            stat = os.stat(fpath)
        except OSError:
            continue

        size_bytes = stat.st_size
        size_kb = size_bytes / 1024
        total_kb += size_kb
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        age_hours = (now_utc - mtime).total_seconds() / 3600

        label, desc = _MEMORY_FILE_LABELS.get(fname, (fname.replace(".json", ""), ""))
        entry_count = _count_json_entries(fpath)

        # Health status — exempt files skip age checks (intentionally infrequent)
        age_exempt = fname in _MEMORY_AGE_EXEMPT
        size_crit = size_kb >= _MEMORY_SIZE_CRIT_KB
        size_warn = size_kb >= _MEMORY_SIZE_WARN_KB
        age_crit = not age_exempt and age_hours >= _MEMORY_AGE_CRIT_HOURS
        age_warn = not age_exempt and age_hours >= _MEMORY_AGE_WARN_HOURS

        if size_crit or age_crit:
            health = "crit"
            crit_count += 1
        elif size_warn or age_warn:
            health = "warn"
            warn_count += 1
        else:
            health = "ok"

        if age_hours < 1:
            age_str = f"{int(age_hours * 60)}m ago"
        elif age_hours < 24:
            age_str = f"{age_hours:.1f}h ago"
        else:
            age_str = f"{age_hours / 24:.1f}d ago"

        rows.append(
            {
                "fname": fname,
                "label": label,
                "desc": desc,
                "size_kb": size_kb,
                "entry_count": entry_count,
                "age_str": age_str,
                "age_hours": age_hours,
                "age_exempt": age_exempt,
                "health": health,
                "size_warn": size_warn or size_crit,
                "age_warn": age_warn or age_crit,
            }
        )

    # Summary strip
    ok_count = len(rows) - warn_count - crit_count
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

    # File table using HTML for compact display
    health_icons = {"ok": "🟢", "warn": "🟡", "crit": "🔴"}

    rows_html = []
    for r in rows:
        icon = health_icons[r["health"]]
        size_str = f"{r['size_kb']:.1f} KB"
        size_color = (
            "#f44336"
            if r["size_kb"] >= _MEMORY_SIZE_CRIT_KB
            else "#ff9800" if r["size_kb"] >= _MEMORY_SIZE_WARN_KB else "#888"
        )
        age_color = (
            "#f44336"
            if r["age_hours"] >= _MEMORY_AGE_CRIT_HOURS
            else "#ff9800" if r["age_hours"] >= _MEMORY_AGE_WARN_HOURS else "#888"
        )
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
            icon = health_icons[r["health"]]
            reasons = []
            if r["size_warn"]:
                reasons.append(f"large ({r['size_kb']:.0f} KB)")
            if r["age_warn"]:
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
        ws_mb = info.get("workspace_mb")
        if ws_mb is not None:
            st.metric("Workspace", f"{ws_mb} MB")

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
                file_name="portal_errors.json",
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

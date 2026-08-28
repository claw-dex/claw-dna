# BOOTSTRAP: First Cycle — Evolve Toward Your First Goal

> **Enum Reference:** See `prompts/enum.md` for valid `agent_status` values used during bootstrap.

This is your very first cycle. The multi-service gateway is running:
Caddy (port 8080) and Streamlit (port 8081 at /app/).
Your task: initialize your memory and evolve the agent to serve the user's first goal.

## Step 0: Check for Migration Backup

Before anything else, capture the migration backup path (if any) from
`/agent/memory/portal_config.json`:

```bash
CFG=/agent/memory/portal_config.json
BACKUP_ZIP=$([ -f "$CFG" ] && jq -r '.bootstrap_backup_path // ""' "$CFG" || echo "")
if [ -n "$BACKUP_ZIP" ]; then
  echo "Migration mode — restoring from: $BACKUP_ZIP"
else
  echo "Normal first-cycle mode — no migration backup"
fi
```

**If `$BACKUP_ZIP` is non-empty**, this is a **migration cycle**, not a normal
first-cycle evolution. The user uploaded an `agent_full_backup_*.zip` via the
portal's first-run form, and you must restore from it instead of building
something new.

### Step 0.1 — Remember the user's submitted goal

The first goal entry user submitted is already in your prompt context under `<your_goals>`.

**Remember its full text now** — Phase 2 of Recovery will overwrite
`goal.json` with the backup's contents, erasing this entry. You will write it back in Step 0.3.

The goal may be either:

- The **synthetic** goal the portal wrote when only a backup was uploaded
  (e.g. `"Migrate agent state from uploaded backup: <name>"`), or
- The **user's typed goal**, which is a post-migration instruction (e.g.
  `"Migrate, but skip rebuilding long-term memory; then add a Trading tab"`).

If the goal contains directives that change *how* migration is performed
(skip steps, alter ingest flags, etc.), honor them when executing the Phases
below.

### Step 0.2 — Run Recovery

Follow **Phases 1–5 of the Recovery section** in
`skills/full-backup-and-migrate/SKILL.md` exactly, using `$BACKUP_ZIP`
as the `BACKUP_ZIP` variable referenced throughout the skill. Skip or modify
individual phases only if the preserved goal explicitly tells you to (and log
what you skipped in the cycle-close summary).

### Step 0.2b — Verify and Fix Streamlit Portal Health

After Recovery completes, you **must** verify that the Streamlit portal is fully functional and accessible. Since a migration restores files, dependencies, and configurations from another environment, conflicts or mismatches can easily break the Streamlit app, locking the user out of the agent portal.

Run the following checks and fix any issues before proceeding:

1. **Verify local health endpoint and page content**:
   ```bash
   curl -s http://localhost:8081/app/_stcore/health
   curl -s http://localhost:8081/app/
   ```
   Confirm that the health endpoint returns `ok` and the home page returns HTML content.

2. **Run headless render check**:
   ```bash
   uv run python scripts/app_check.py
   ```
   This validates that `server.py` and all tab modules import correctly, render without exceptions, and can write successfully to the messages inbox.

3. **Resolve common portal issues**:
   * **Dependency/Import Errors**: If the check reports missing packages or import errors, verify that `pyproject.toml` and `uv.lock` were restored correctly and run:
     ```bash
     uv sync
     ```
   * **Syntax / Runtime Errors**: Check the output and logs. If there are syntax or runtime exceptions (often caused by unresolved git merge conflicts, missing files, or code drift), inspect the offending files in `app/` or `server.py` and fix the code.
   * **Port/Service Unreachable**: If the server is unreachable, verify service status:
     ```bash
     uv run python scripts/service_manager.py status
     ```
     Ensure both Streamlit and Caddy services are running. Check portal config `/agent/memory/portal_config.json` and Caddyfile settings.

Do **not** proceed to Step 0.3 or close the cycle until the Streamlit portal is healthy and passes `scripts/app_check.py`.

### Step 0.3 — Restore the preserved goal

After Recovery completes, `goal.json` reflects the source-agent's goal history.
Append the goal text you remembered in Step 0.1 with a fresh `id` (to avoid
collision with restored entries) and `status: in_progress` — you will work on
it within this same bootstrap cycle and finalize its status in Step 0.5:

```bash
GOAL_FILE=/agent/memory/goal.json
# Substitute the goal text you remembered from Step 0.1:
GOAL_TEXT='<paste the original first-goal text here>'
NEXT_NUM=$(jq '[.[].id | capture("goal-(?<n>\\d+)"; "g") | .n | tonumber] | (max // 0) + 1' "$GOAL_FILE")
NEW_ID="goal-${NEXT_NUM}"
NOW=$(date -u +%Y-%m-%dT%H:%M:%S%z)
tmp="${GOAL_FILE}.tmp"
jq --arg goal "$GOAL_TEXT" --arg id "$NEW_ID" --arg ts "$NOW" \
   '. + [{id:$id, goal:$goal, status:"in_progress", created_at:$ts, source:"migration-preserved"}]' \
   "$GOAL_FILE" > "$tmp" && mv "$tmp" "$GOAL_FILE"
echo "Re-appended preserved goal as $NEW_ID"
```

### Step 0.4 — Clear the migration marker

Prevent re-fired heartbeats from re-triggering migration:

```bash
CFG=/agent/memory/portal_config.json
tmp="${CFG}.tmp"
jq 'del(.bootstrap_backup_path)' "$CFG" > "$tmp" && mv "$tmp" "$CFG"
```

### Step 0.5 — Act on the preserved goal and finalize its status

Work on the goal **within this same bootstrap cycle**. Using the goal text you
remembered in Step 0.1 (now persisted as `$NEW_ID` in `goal.json` with
`status: in_progress`), decide whether it is:

- **Synthetic-only** (`"Migrate agent state from uploaded backup: …"`) → no
  further work required. If migration succeeded, update `$NEW_ID` to
  `status: completed`. If migration failed or any phase produced unresolved
  errors, update to `status: failed`.
- **User-supplied with post-migration instructions** → execute those
  instructions now (portal edits, capability installs, scripts, etc., similar
  to Steps 2–3 below but driven entirely by the preserved goal). When done,
  update `$NEW_ID` to `status: completed` if everything succeeded, or
  `status: failed` if any required step could not be completed.

Use the same `jq` pattern as Step 6 below to flip the status (substituting
`$NEW_ID` instead of the first goal).

### Step 0.6 — Cycle-close and stop

Before closing, run `uv run python scripts/app_check.py` one last time to ensure no post-migration changes or edits broke the portal. 

Run `cycle_close.py` with `--type evolve --category capability` and a summary
describing what was restored, any post-migration work performed, and the final
status of `$NEW_ID`. Then **stop bootstrap here.** Do **not** run Steps 1–6 —
memory, capabilities, state, prompts, and skills were just restored from
backup and re-running first-cycle evolution would clobber them.

---

**If `$BACKUP_ZIP` is empty**, continue with Step 1 below as the normal
first-cycle evolution. Migration mode and first-goal mode are mutually
exclusive paths through this bootstrap.

## Step 1: Read Your Goal

The user's first goal is in `/agent/memory/goal.json` (appended below this document).
Read it carefully — it defines what kind of agent you should become during this bootstrap.

Skim what already exists:

- Current capabilities (`/agent/memory/capabilities.json`)
- TAB_REGISTRY in `server.py` — understand the current portal layout

## Step 2: Plan Your Bootstrap Evolution

Based on the user's first goal, decide what needs to change to make the agent useful for
that goal. Consider:

1. **Portal UI** — Does the current Streamlit UI serve this goal? Check TAB_REGISTRY in
   `server.py` and the modules in `app/`. If not, redesign by editing `server.py` and/or
   `app/*.py`. Streamlit's hot-reload (`runOnSave = true`) applies changes automatically.
2. **Capabilities** — What tools, scripts, or integrations does the agent need? Install
   packages via `pyproject.toml` + `uv sync`. Create utility scripts in `/agent/scripts/`.
3. **Memory structure** — Does `state.json` need extra fields for tracking goal-specific state?
4. **Prompts** — Do existing prompts (goal.md, evolve.md, research.md) need updates to
   handle goal-related tasks properly?

Pick ONE high-impact improvement that directly serves the goal. Do it well.

**Always update the Streamlit page emoji** in `server.py` to one that best
reflects the agent's character for this goal. Replace the `page_icon` value
in the `st.set_page_config(...)` call (and the `page_title` if a new title
fits better):

```python
# ── Page config (must be first Streamlit call) ────────────────
st.set_page_config(
    page_title="Autonomous AI Agent",  # ← pick a title that best reflects the agent's character. If you are given a name in the bootstrap goal, the title must include it.
    page_icon="🚀",  # ← pick a fresh emoji that fits this agent's character. If you are given an emoji in the bootstrap goal, you must use it here.
    layout="wide",
    initial_sidebar_state="collapsed",
)
```

**Always update the portal theme** to match the agent's character for this
goal. The default theme (`base = "dark"` only) is a placeholder — replace it
with a deliberate choice that reinforces the goal's tone.

> **Skip this entire sub-step if you reached this point from migration
> recovery.** Migration mode halts at Step 0.6 and never executes Step 2, so
> in practice this skip is automatic — but if the preserved goal in Step 0.5
> directs you to perform post-migration portal work, do **not** retheme as
> part of it unless the user explicitly asked for a theme change. Restored
> backups carry the user's prior theme and must not be overwritten.

How to choose:

1. Infer the desired tone from the first goal — e.g. a trading agent → high-
   contrast/Stripe/Snowflake; a creative writing assistant → Solarized-Light
   or a warm custom palette; a security tool → Dracula or Nord; a brand-
   specific agent → a custom theme using the brand's primary color.
2. Use the `change-portal-theme` skill at `skills/change-portal-theme/SKILL.md`
   for the exact mechanics. It documents the option reference and the eight
   bundled presets at `skills/developing-with-streamlit/templates/themes/`
   (`dracula`, `github`, `minimal`, `nord`, `snowflake`, `solarized-light`,
   `spotify`, `stripe`).
3. Edit **only** the `[theme]` block (and `[[theme.fontFaces]]` if needed) in
   `.streamlit/config.toml`. Leave `[server]` and `[browser]` untouched.
4. If the goal explicitly names a brand, color, or aesthetic, honor it
   directly instead of picking a preset.
5. `git add .streamlit/config.toml` after the edit (Step 3 covers
   git-tracking).

If `[[theme.fontFaces]]` is added, the portal must be restarted for the new
font face to load — see the `change-portal-theme` skill's checklist.

## Step 3: Execute

Implement your chosen improvement:

- **If modifying the portal:** edit `server.py` and/or `app/*.py`, then verify health
- **If adding scripts:** create in `/agent/scripts/`, make executable, test
- **If installing packages:** update `pyproject.toml`, run `uv sync`
- **Git-track every change:** After creating or modifying any file, immediately `git add` it:

  ```bash
  git add path/to/changed/file
  ```

After making changes, verify the portal is still healthy:

```bash
curl -s http://localhost:8081/app/_stcore/health
uv run python scripts/app_check.py
```

## Step 4: Initialize State

Update `state.json` to reflect the in-progress bootstrap:

```bash
uv run python -c "
import json, datetime
with open('/agent/memory/state.json') as f:
    s = json.load(f)
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
s.update({'cycle_number': 0, 'status': 'bootstrapping',
          'last_cycle_summary': 'Bootstrap: evolved agent toward first goal',
          'last_heartbeat': now, 'last_cycle_run': now})
import tempfile, os
tmp = '/agent/memory/state.json.tmp'
with open(tmp, 'w') as f:
    json.dump(s, f, indent=2)
os.replace(tmp, '/agent/memory/state.json')
print('state.json updated')
"
```

**Note:** `cycle_number` is set to `0` (not 1) during bootstrap because it represents the last *completed* cycle.

Update `/agent/memory/capabilities.json` with:

- What you built or installed this cycle
- Tools and languages available
- Services running

## Step 5: Verify State

Confirm `state.json` was written correctly:

```bash
uv run python -c "
import json
with open('/agent/memory/state.json') as f:
    s = json.load(f)
assert 'cycle_number' in s, 'cycle_number not set'
assert s.get('agent_status'), 'agent_status missing'
print('state.json OK:', json.dumps({k: s[k] for k in ['cycle_number', 'agent_status', 'last_heartbeat', 'last_cycle_run']}, indent=2))
"
```

## Step 6: Complete the Bootstrap Goal

Mark the first goal (the one the user submitted via the portal) as completed:

```bash
uv run python -c "
import json, os
path = '/agent/memory/goal.json'
with open(path) as f:
    goals = json.load(f)
if goals and goals[0].get('status') in ('pending', 'in_progress'):
    goals[0]['status'] = 'completed'
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(goals, f, indent=2)
    os.replace(tmp, path)
    print('First goal marked as completed')
else:
    print('No pending first goal found')
"
```

This ensures the bootstrap goal does not carry over into subsequent cycles as an active goal.

## End of Cycle

**Commit all staged changes** before running cycle-close.

The bootstrap cycle is cycle `#0`. Include the cycle number and a detailed, descriptive commit message that explains what was built, why it serves the user's goal, and how it sets the foundation for future evolution cycles. Use multi-line commit messages:

```bash
git commit -m "$(cat <<'EOF'
#0 bootstrap: <what you built for the goal>

Goal: <the user's first goal, summarized>
Reason: <why this specific improvement was chosen to serve the goal>

Changes:
- <detailed description of each change made>
- <what files were modified/created and why>
- <packages installed or tools configured>

Bootstrap context:
- <how this sets up the agent for future evolution>
- <what capabilities are now available that weren't before>
- <any design decisions made and their rationale>
EOF
)"
```

Then follow the `/agent/prompts/cycle-close.md` checklist. Use `cycle_close.py` to record this cycle:

```bash
uv run python scripts/cycle_close.py \
    --type evolve \
    --category capability \
    --goal "Bootstrap: <what you set out to build for this cycle>" \
    --summary "Bootstrap: <what you actually delivered and why it serves the goal>" \
    --actions "Action 1" "Action 2" "Action 3"
```

`--goal` is the *planned intent* (decided before/at cycle start); `--summary` is the
*delivered outcome*. Keep them distinct — see `prompts/cycle-close.md`.

Ensure auto memory `capabilities.json` was updated in Step 4 with any new tools/capabilities.

## Rules

- Pick ONE improvement per cycle. Do it well.
- Test changes before finishing. Verify portal health after any server.py edit.
- Let the user's goal guide every decision — don't build generic infrastructure when
  goal-specific capability is more valuable.

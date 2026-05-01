# BOOTSTRAP: First Cycle — Evolve Toward Your First Goal

> **Enum Reference:** See `prompts/enum.md` for valid `agent_status` values used during bootstrap.

This is your very first cycle. The multi-service gateway is running:
Caddy (port 8080) and Streamlit (port 8081 at /app/).
Your task: initialize your memory and evolve the agent to serve the user's first goal.

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

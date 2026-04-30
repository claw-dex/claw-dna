# Cycle Close Checklist

> **Enum Reference:** See `prompts/enum.md` for all valid values of `type`, `category`, `status`, and other enum fields.

Run this checklist at the end of every cycle, regardless of cycle type.

## RUN-ONCE RULE — read this before doing anything

> **`cycle_close.py` MUST be run at most ONCE per cycle.** Running it twice creates a
> duplicate journal entry, double-archives the outbox, and corrupts duration tracking.

Before invoking `cycle_close.py`, **check whether the current cycle has already been closed**:

```bash
uv run python -c "
import json
cs = json.load(open('/agent/memory/cycles.json'))
ip = [c for c in cs if c.get('status') == 'in_progress']
if not ip:
    print('NO_IN_PROGRESS — cycle already closed (or never started). DO NOT run cycle_close.py again.')
else:
    last = max(ip, key=lambda c: c.get('cycle', 0))
    print(f'IN_PROGRESS cycle={last[\"cycle\"]} — proceed with cycle_close.py')
"
```

- `NO_IN_PROGRESS` → **stop**. The cycle is already closed. Skip to steps 3 and 5 only
  (portal health + error clearing). Do NOT run `cycle_close.py`.
- `IN_PROGRESS cycle=<N>` → first close attempt for this cycle. Proceed below.

If you already ran `cycle_close.py` earlier in this same heartbeat, you do **not** need
to consult this file again — it's done.

## 1. Run cycle_close.py (only when an in-progress cycle exists)

```bash
uv run python scripts/cycle_close.py \
    --type evolve \           # evolve | goal | self-heal | dream (see prompts/enum.md → Cycle Type)
    --category efficiency \   # required for evolve and dream cycles (see prompts/enum.md → Evolution Category)
    --summary "What you actually delivered and why it matters (the outcome)" \
    --actions "Action 1" "Action 2" "Action 3"
```

**One command handles all of this** — do NOT do any of these by hand:

- creates / updates the `cycles.json` entry (start, end, duration, status, summary, type, category)
- normalizes legacy cycles.json fields (`timestamp`→`start`, `goal`→`summary`)
- updates `state.json` (`cycle_number`, `status`, `current_goal`, `last_cycle_summary`, `last_cycle_end`, …)
- appends the `journal.json` entry (cycle, timestamp, status, type, category, goal, actions, summary)
- archives `outbox.json` and clears it
- runs `memory_backup.py` if the last backup is >1h old
- runs the stale-count check (tabs / tests / scripts vs AGENTS.md and prompts)

`--cycle` is auto-detected from the in-progress entry; `--dry-run` previews.

### Notes on `--goal`

- The planned goal is normally read from the in-progress cycle entry — set by
  `cycle_start.py --goal "..."` (goal mode) or by patching `cycles.json` after picking
  the category (evolve mode). When present there, **omit `--goal`** at close.
- Pass `--goal "..."` only if (a) no goal was recorded at start, or (b) the actual work
  diverged from the planned goal.
- **Never copy `--summary` into `--goal`.** Goal = the plan; summary = the outcome.
  They must differ. Omitting `--goal` is preferable to duplicating the summary.

### Journal entry self-test

After `cycle_close.py` runs, glance at the new entry it wrote:
*"If I read this with no memory next cycle, would I understand what happened?"*
If not, re-run with a sharper `--summary` (use `--cycle <N>` to overwrite the same entry —
this counts as the same close, not a second one).

## 2. Update outbox (BEFORE running cycle_close.py if you have new info for the user)

If there's new information for the user (goal complete, question, status update), write
it to `/agent/messages/outbox.json` **before** running `cycle_close.py`.

**Do NOT clear or archive outbox.json yourself** — `cycle_close.py` archives it. The user
will read and clear messages manually via the portal's "Clear All" button.

**Required outbox content for completed goals:**

- What was done (1-2 sentences)
- Where to find outputs (file paths, portal tab, or URL)
- Next steps the user should take (if any)

## 3. Verify portal (if web files changed)

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/app/_stcore/health  # expect 200
```

## 4. Update AGENTS.md and capabilities (if you added new skills/tools)

If you added scripts, portal modules, or commands/skills this cycle:

1. Add the new script/module to the **Utility Scripts** section in `AGENTS.md`
   so future cycles can discover it via the briefing.
2. Update `/agent/memory/capabilities.json` if needed.

## 5. Clear resolved tab errors (optional)

If you fixed tab errors this cycle, clear them before the next briefing:

```bash
# Reliable direct clear (always works):
python3 -c "import json; open('/agent/memory/server_errors.json','w').write('[]')"

# Or via cycle-start flags (next heartbeat):
uv run python scripts/cycle_start.py --clear-all-errors    # all errors
uv run python scripts/cycle_start.py --clear-old-errors    # only >1h old
```

Only run if you actually fixed the root cause — don't clear errors that may still recur.

## Schema reference (for emergencies only)

`cycle_close.py` does all of the file writes below automatically. The schemas are
documented here only so you can reconstruct an entry by hand if `cycle_close.py` is
unavailable (broken script, missing dependency, etc.). **Do not run these snippets in a
normal close — they will conflict with `cycle_close.py`.**

- **`/agent/memory/cycles.json`** — list of `{cycle, start, end, duration_seconds, type, status, summary}`
  plus `category` for evolve/dream cycles.
- **`/agent/memory/state.json`** — `{cycle_number, status, current_goal, goal_status, last_cycle_summary, last_cycle_end, last_cycle_type}`.
- **`/agent/memory/journal.json`** — append `{cycle, timestamp, status, type, category?, goal?, actions, summary}`.
  Use `summary` (not `outcome`) — `cycle_start.py` reads `summary` when rendering the briefing.
  Omit `category` for `goal` and `self-heal`. Omit `goal` if no plan was recorded — never duplicate `summary`.

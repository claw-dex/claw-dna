# Cycle Close Checklist

> **Enum Reference:** See `prompts/enum.md` for all valid values of `type`, `category`, `status`, and other enum fields.

Run this checklist at the end of every cycle, regardless of cycle type.

## FAST PATH — Use cycle_close.py (recommended)

Instead of running steps 1–3 manually, use the automation script:

```bash
uv run python scripts/cycle_close.py \
    --type evolve \           # evolve | goal | self-heal (see prompts/enum.md → Cycle Type)
    --category efficiency \   # required for evolve cycles (see prompts/enum.md → Evolution Category)
    --summary "One or two sentence summary of what was done and why it matters" \
    --actions "Action 1" "Action 2" "Action 3"
```

`--cycle` is **optional** — auto-detected as `state.cycle_number + 1` (override only if wrong).
This handles cycles.json + state.json + journal.json + normalize + outbox archive + stale-count check + memory backup in one command.
Use `--dry-run` to preview before writing. Still do steps 4 and 9 manually (portal health + error clearing).

## 0. Record cycle start (if not already done)

`cycle_close.py` handles this automatically — it creates the entry if it doesn't exist.
**No manual action needed** unless you explicitly want a stub at cycle start for long-running cycles.

If you do need a manual start stub (rare):
```bash
# Just run cycle_close.py at the end — it creates + completes the entry in one step
uv run python scripts/cycle_close.py --cycle <N> --type evolve --category <CAT> --summary "..."
```

## 1. Update cycles.json entry

> **→ Use the FAST PATH above** (`cycle_close.py`) — it handles this automatically.
> The code below documents the schema only; use it only if `cycle_close.py` is unavailable.

Find this cycle's entry and mark it completed with timing:

```python
import json, datetime
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
cycles = json.load(open('/agent/memory/cycles.json'))
for c in cycles:
    if c.get("cycle") == <N>:
        c["end"] = now
        c["duration_seconds"] = <elapsed_seconds>  # end - start
        c["status"] = "completed"                  # or "failed"
        c["summary"] = "<final 1-2 sentence summary>"
        break
open('/agent/memory/cycles.json', 'w').write(json.dumps(cycles, indent=2))
```

Required fields on every completed entry:
`cycle`, `start`, `end`, `duration_seconds`, `type`, `status`, `summary`
Plus `category` for evolve cycles (reliability | observability | capability | efficiency | prompt_evolution).

## 1b. Normalize cycles.json (automatic)

`cycle_close.py` automatically normalizes cycles.json on every run (inlined logic).
This fixes: `timestamp` → `start`, `goal` → `summary`, computes `duration_seconds` where possible.
Use `--no-normalize` to skip if needed.

## 2. Update state.json

Set these fields:
- `cycle_number`: current cycle number
- `status`: "idle" (or "working" if goal continues next cycle)
- `current_goal`: what you worked on (or null)
- `goal_status`: "completed" / "in_progress" / null
- `last_cycle_summary`: 1-2 sentence summary of what you did
- `last_cycle_end`: current timestamp (set automatically by cycle_close.py)

## 3. Write journal entry

Append to `/agent/memory/journal.json`:

```python
import json, datetime
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
journal = json.load(open('/agent/memory/journal.json'))
journal.append({
    "cycle": <N>,
    "timestamp": now,
    "status": "completed",       # completed | failed | in_progress
    "type": "evolve",            # evolve | goal | self-heal
    "category": "...",           # evolve only: reliability | observability | capability | efficiency | prompt_evolution
    "goal": "<what you did>",
    "actions": ["action 1", "action 2"],
    "summary": "<1-2 sentences on result and why it matters>"
})
open('/agent/memory/journal.json', 'w').write(json.dumps(journal, indent=2))
```

**IMPORTANT:** Use `summary` (not `outcome`) — cycle_start.py reads `summary` when displaying recent journal entries. Using `outcome` will cause blank lines in the cycle briefing.

Omit `category` for `goal` and `self-heal` entries.

Self-test: "If I read this entry next cycle with no memory, would I understand what happened?"

## 4. Verify portal (if web files changed)

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8081/app/_stcore/health  # expect 200
```

## 5. Update outbox

If there's new information for the user (goal complete, question, status update), write
it to `/agent/messages/outbox.json` **before** running `cycle_close.py`.

**Do NOT clear or archive outbox.json** — the user will read and clear messages manually
via the portal's "Clear All" button (which archives to `outbox_history.json` first).

**Required outbox content for completed goals:**
- What was done (1-2 sentences)
- Where to find outputs (file paths, portal tab, or URL)
- Next steps the user should take (if any)

## 6. Create memory backup (automated — cycle_close.py does this for you)

**As of cycle 167, `cycle_close.py` runs a memory backup automatically** when the last
backup is >1h old. cycle_start.py shows ⚠ STALE when the last backup is older than 1 hour —
this step keeps that warning quiet and protects against data loss.

**No manual action needed** — the backup status is reported in cycle_close.py output.

If you need to run manually (e.g., cycle_close.py is unavailable):
```bash
uv run python scripts/memory_backup.py
```

Auto-prunes at 20 snapshots, so storage is not a concern.

## 7. Check for stale counts (automated — cycle_close.py does this for you)

**As of cycle 114, `cycle_close.py` runs this check automatically every cycle.**
It compares actual tab count (TAB_REGISTRY), test count (self_test.py), and script count
against AGENTS.md and prompts — printing `[STALE COUNTS DETECTED]` warnings only when drift
is found. Silent when everything matches.

**No manual action needed** unless cycle_close.py reports a mismatch.

If you need to check manually (e.g., cycle_close.py is unavailable):
```bash
# Tab count
python3 -c "src=open('/agent/server.py').read(); idx=src.find('TAB_REGISTRY = ['); body=src[idx:]; n=body[:body.find(']')].count('('); print(f'{n} tabs')"
# Script count
ls /agent/scripts/*.py /agent/scripts/*.sh 2>/dev/null | wc -l
# Test count
uv run python scripts/self_test.py --quiet 2>&1 | tail -1
```

## 8. Update AGENTS.md and auto memory (if you added new skills/tools)

If you added scripts, portal modules, or commands/skills this cycle:

1. Add the new script/module to the **Utility Scripts** section in `AGENTS.md`
   so future cycles can discover it via the briefing.
2. Update `capabilities.md` in auto memory if needed (auto memory is synced
   automatically by `cycle_close.py` for state, cycles, journal, and goals).

## 9. Clear resolved tab errors (optional)

If you fixed tab errors this cycle, clear them before the next briefing:

```bash
# Reliable direct clear (always works):
python3 -c "import json; open('/agent/memory/server_errors.json','w').write('[]')"

# Auto-clear via cycle-start (may fail silently if Streamlit re-writes the file):
uv run python scripts/cycle_start.py --clear-all-errors

# Clear only errors >1h old (if you're unsure whether they're fixed):
uv run python scripts/cycle_start.py --clear-old-errors
```

Only run if you actually fixed the root cause — don't clear errors that may still recur.

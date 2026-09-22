# EVOLVE: Self-Improvement Cycle

> **Enum Reference:** See `prompts/enum.md` for all valid values of `category`, `status`, and `type` used in evolve cycles.

No task is assigned. Your goal: become a more capable agent.

## Step 1: Run Cycle Start

```bash
git log --oneline -20
uv run python scripts/cycle_start.py --mode evolve    # status, past goals, recent journal, repair + evolve recommendation
```

Review the git log to understand what past cycles have changed — avoid repeating recent work
and build on what's already been done. Then review the cycle-start output.

**Goal is set after cycle start, not at start.** Unlike `goal` mode, the planned goal
isn't known until you've reviewed the `[EVOLVE RECOMMENDATION]` and chosen what to work
on. Once you've picked a category, patch the in-progress entry in `cycles.json` to set
`cycle_goal` (so the cycle record carries the plan), or pass `--goal "..."` to
`cycle_close.py` at the end of the cycle (see Step 5 below). The goal describes the
*plan* (what you took on); the summary describes the *outcome* (what you delivered).
Keep them distinct — see `prompts/cycle-close.md` for the rule.

This outputs your current state, recent journal, failures, AND the evolve recommendation
with **dynamic score-based analysis**. The `[EVOLVE RECOMMENDATION]` section shows:

- A **score per category** (0–100) computed from 5 signals
- The final `→ Suggest:` category (highest score wins)

**Trust the suggestion directly** — the scoring system already factors in balance, recency,
goal alignment, ROI, and maturity. No separate balance check needed.

Then skim:

- Active operations: Check `/agent/messages/inbox.json` for `[DNA UPDATE IN PROGRESS]`. If a recent (< 15m) DNA update is underway (and neither `[DNA UPDATE COMPLETE]` nor `[DNA UPDATE ABORTED]` is present), **DO NOT modify files, do NOT attempt evolve changes, and do NOT abort or interfere with git rebase**. Stand down and close the cycle cleanly. If complete/aborted or stale (> 15m), proceed normally.
- The `<your_past_goals>` section of this prompt — the most recent 20 completed/failed goals (pre-extracted from `goal.json`). Use it to avoid repeating work already done and to build on prior outcomes. Do **not** re-parse `goal.json` for this view.
- Failure patterns from journal entries — fix patterns, not symptoms
- Avoid repeating work in recent journal entries — they maybe be part of evolve cycles
- Current capabilities (`/agent/memory/capabilities.json`) — know what already exists
- The `[RECENT MEMORY FILES]` section of the cycle-start output — paths to topic/learning files written by last night's dream (`/agent/memory/dream/{learnings,topics}/`). Read any whose slug looks relevant to the failures or category you're weighing. Together with `[LONG-TERM MEMORY]` (>24h), this is your full memory window.

## Step 1.5: Check for Human Escalation

Read `/agent/memory/state.json` and check if the `agent_status` field equals `"waiting_for_human"`.

If status is `"waiting_for_human"`:

1. Extract the `last_cycle_summary` field from state.json to use as the call message
2. Run `uv run python scripts/callmebot.py --json status` to check if configured and not rate-limited
3. If `can_call_now` is true, make a voice call to escalate to the user:

   ```bash
   uv run python scripts/callmebot.py call --text "<last_cycle_summary from state.json>"
   ```

4. Then proceed with the cycle normally

If CallMeBot is not configured or is rate-limited, skip the call and proceed with the cycle.
See the `callmebot` skill for setup instructions and full details.

## Step 2: Check & Fix Tab Errors

1. Check `/agent/messages/inbox.json`: if a recent (< 15m) `[DNA UPDATE IN PROGRESS]` is present without complete/aborted notices, **DO NOT fix tab errors, modify code, or abort git rebase**. Portal disruption during upstream DNA sync is expected. Defer evolve work and proceed to cycle close. (If complete/aborted or stale > 15m, proceed with fixes).
2. Read `/agent/memory/server_errors.json`
3. Run the app render check to catch runtime/import errors that `server_errors.json` might miss:

   ```bash
   uv run python scripts/app_check.py
   ```

   - `[app-check] OK` → no runtime errors
   - `[app-check] FAIL` → fix the reported errors before continuing
4. If both `server_errors.json` is empty (`[]`) and app-check passes, skip to Step 4
5. If errors exist:
   a. Read each traceback to identify the root cause (which tab/module, what line)
   b. Fix the root cause in the affected file (e.g., `app/*.py`, `server.py`)
   c. After fixing, verify with both checks:

      ```bash
      curl -s http://localhost:8081/app/_stcore/health
      uv run python scripts/app_check.py
      ```

   d. Once verified, clear the errors:

      ```bash
      uv run python -c "import json; open('/agent/memory/server_errors.json','w').write('[]')"
      ```

6. Only clear errors you've confirmed are fixed — stale errors from a hot-reload are always safe to clear

**Important:** If errors are stale (timestamp predates your most recent portal fix in the journal), they are safe to clear without further investigation.

## Step 3: Choose Your Category

Pick the `→ Suggest:` category from the `[EVOLVE RECOMMENDATION]` section.

### How Scoring Works

Each category gets a score from 5 signals (typically 0–90, can be negative for mature areas):

| Signal | Range | What it measures |
|--------|-------|------------------|
| **base_need** | 0–30 | How underserved vs ideal 20% share |
| **recency_boost** | 0–25 | How many evolve cycles since last picked |
| **goal_alignment** | 0–25 | Do unfinished goals need this category |
| **roi_bonus** | 0–10 | Historical success rate |
| **maturity_penalty** | 0–40 | Graduated penalty for mature areas |

The category with the highest score is suggested. The system automatically:

- Rotates away from recently-picked categories (via recency_boost)
- Prioritizes categories that serve unfinished goals (via goal_alignment)
- Gradually deprioritizes mature areas instead of hard-skipping them
- Rewards categories with high success rates

### Overriding the Suggestion

The **only valid reason to override** is an active failure or error requiring immediate
attention (e.g., a broken tab forces reliability even if its score is low).
If you override, document why in the cycle-close journal entry.

**High-value improvements that are easy to overlook:**

- Check routing tables in goal.md and research.md — are newly-added scripts listed there?
- Check message handler classification branches in goal.md — does every likely user question type have a handler?
- Check prompts for stale/incorrect information (wrong script flags, outdated counts, removed features)
- If you add a NEW data loader to `app/data.py`, add it to the correct table in `prompts/server.md` (mtime-based or TTL-based) and update the cycle range note in the Data Layer header.
- Write a new prompt for a situation that caused confusion (check journal)
- Improve user-facing error messages in the portal
- Install or create new skills/commands for repeated manual tasks (see `/agent/skills/`)
  - `developing-with-streamlit` - use it when building or modifying the portal. It routes to sub-skills for layouts, performance, chat UI, custom components, themes, and more. Activate it by using `/developing-with-streamlit` or let it auto-activate on Streamlit development work.
  - `skills-sh-find-skills` - use it to discover and install new skills from the open agent skills ecosystem (<https://skills.sh/>). Good for when a cycle needs a capability that might already exist as a community skill. When it returns a good match relevant to the current cycle's needs and the install's count is good enough, install the found skill automatically without asking for user's permission.
- Improve memory file formats so future cycles parse faster

## Improvement Categories

> See `prompts/enum.md` → Evolution Category for complete category definitions and maturity penalties.

### Reliability

Fix bugs in portal (server.py or apps/*.py), or memory system (json files or scripts). Add error handling, graceful degradation.
**Good use:** A tab is crashing, a script has an edge-case bug, memory files are inconsistently structured.
**Maturity penalty applies when:** No failures in recent cycles (penalty 15).

### Observability

Improve the portal so the user understands agent state. Add visualizations, timelines, metrics.
**Good use:** Key data is buried or confusing, user can't easily see what the agent is doing.
**Maturity penalty applies when:** Portal has 20+ tabs (graduated, up to penalty 20 at 25+ tabs).

### Capability

Build utility scripts (`/agent/scripts/`) or install new tools. Make things future cycles can reuse.
**Good use:** The agent can't yet do something a user might reasonably ask — e.g., no script for a common task, no tool for a frequently needed lookup, or a gap in the goal routing table.
**Maturity penalty applies when:** 25+ capabilities registered (graduated, up to penalty 20 at 30+).

### Efficiency

Reduce wasted cycles. Optimize hot paths (cycle-start, server load, memory parsing).
**Good use:** A hot path runs slower than it should (e.g., a cache re-reads on every request, a script takes >10s for a simple task, or a prompt causes the agent to repeat work already done).
**Maturity penalty applies when:** mtime conversion complete and `server.py` < 500 lines (penalty 15).

### Prompt Evolution

Refine existing prompts, create new ones for uncovered situations, remove outdated instructions.
Always read a prompt before modifying it. Keep prompts concise — trim, don't pad.
**No maturity penalty** — prompt evolution is always eligible.

## Rules

- Pick ONE improvement per cycle. Do it well.
- **Never interrupt active DNA update:** If `/agent/messages/inbox.json` contains `[DNA UPDATE IN PROGRESS]` (or `from.source: "claw-update-dna"`), do NOT modify files, stage git changes, or run `git rebase --abort`/checkout/reset commands.
- **ONE cycle per heartbeat.** Never run `cycle_start.py` or `cycle_close.py` more than once
  per session. Never create additional cycle entries in `cycles.json`. If you discover a
  goal or inbox item while working on an evolve cycle, leave it — the next heartbeat will
  handle it. Creating overlapping cycle entries causes interruptions and lost work.
- Test changes before finishing. Verify portal health after any `server.py` edit.
- **Run app-check after modifying any file in `app/` or `server.py`:**

  ```bash
  uv run python scripts/app_check.py
  ```

  Fix any reported errors before continuing.
- **Run the full test suite after modifying any agent Python file:**

  ```bash
  uv run pytest test/ -v
  ```

  If any tests fail, perform a quick root-cause analysis (RCA) — identify which change
  broke which test and why. Then highlight the failures and notify the user via the
  outbox so the issue isn't silently buried.
- **Tests are mandatory for new Python code:** Every new script in `scripts/` and every
  new function added to an existing Python script MUST come with corresponding unit tests
  in `test/` that cover the new behavior (happy path + meaningful edge cases). No new
  Python code lands without tests.
- **Git-track every change:** After creating or modifying any file, immediately `git add` it:

  ```bash
  git add path/to/changed/file
  ```

## End of Cycle

At end of cycle, **commit all staged changes** before running cycle-close.

Get the current cycle number from `state.json` (`cycle_number` field + 1, since it stores the last *completed* cycle) and include it in the commit message. The commit message must be long and descriptive — explain *what* changed, *why* this category was chosen, and *how* this improvement fits into the agent's evolution. Use multi-line commit messages:

  ```bash
  git commit -m "$(cat <<'EOF'
  #<cycle_number> evolve(<category>): <what changed>

  Category: <category> (score: <score from recommendation>)
  Reason: <why this category was selected — reference the score breakdown and
  any override justification if applicable>

  Changes:
  - <detailed description of each change made>
  - <what files were modified/created and why>

  Evolution context:
  - <how this improvement builds on previous cycles>
  - <what problem or gap this addresses>
  - <expected impact on agent capability/reliability/efficiency>
  EOF
  )"
  ```

Use the category you picked (reliability, observability, capability, efficiency, prompt-evolution) and fill in all sections with specific details from this cycle's work.
Update new capabilities in `capabilities.json` with details of the improvement.

Then follow the `/agent/prompts/cycle-close.md` checklist to close the cycle. Pass
`--goal` to `cycle_close.py` describing the *plan* you set out to do, separately from
`--summary` (the *outcome*):

```bash
uv run python scripts/cycle_close.py \
    --type evolve \
    --category <reliability|observability|capability|efficiency|prompt_evolution> \
    --goal "<one-line plan, e.g. 'evolve(reliability): harden app_check.py against Streamlit reload races'>" \
    --summary "<1-2 sentence outcome, e.g. 'app_check.py now retries health probe up to 3x with 500ms backoff; eliminates the false-FAIL we saw on cycles 712 and 778. Added unit test covering the retry path.'>" \
    --actions "Action 1" "Action 2" "Action 3"
```

`--goal` and `--summary` must be **different** — goal is the intent, summary is the
result. Never copy one into the other. See `prompts/cycle-close.md` for the rule.

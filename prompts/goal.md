# GOAL: Process Inbox or Continue In-Progress Goals

This prompt is triggered when either:
- **New commands** are waiting in /agent/messages/inbox.json, OR
- **In-progress goals** exist in /agent/memory/goal.json (inbox may be empty)

## Cycle Start (run this first)

```bash
uv run python scripts/cycle-start.py        # status, goals, inbox, recent journal, repair + evolve recommendation
```

Review the output before proceeding. If memory-repair reports any fixes, note them in your journal.

## Step 0: Determine Mode

Read `/agent/messages/inbox.json` and `/agent/memory/goal.json`.

- If inbox has items → process them first (see "Processing Rules" below)
- If inbox is empty but goals are in-progress → skip to "Continue In-Progress Goals"
- If both have items → process inbox first, then continue goals

## Command Schema

Commands arrive via POST /api/command with this schema:

```json
{"type": "<string>", "content": "<string>", "timestamp": "<ISO 8601>"}
```

Three command types exist:

- **"goal"** — a trackable objective; queued in /agent/messages/inbox.json.
  You are responsible for saving it to /agent/memory/goal.json (see Goal Tracking below)
- **"message"** — a conversational message (question, context, feedback);
  queued in /agent/messages/inbox.json but NOT tracked in goal.json

## Processing Rules

Read /agent/messages/inbox.json. For each command:

### Command: type "goal"

1. Read the "content" field — this is your new objective
2. If content starts with "abort", immediately stop any current goal,
   set current_goal to null, status to "idle", and write journal entry
3. If content starts with "status", generate a comprehensive status report:
   Read state.json, journal.json, goal.json and compile a summary of current
   goal progress, blockers, and next steps. Write the report to outbox.
4. Otherwise, treat it as a new goal:
   a. Check goal.json for duplicates (see Duplicate Detection below)
   b. If not a duplicate, add the goal entry to goal.json with status "pending"
   c. Set current_goal in state.json to this content
   d. Set status to "working"
   e. Plan the approach (break into steps if complex)
   f. **Route to specialist prompt if needed** (scan the table, pick the best match):

      | Trigger | Action |
      |---------|--------|
      | Needs learning / unknown API or tool | Read `prompts/research.md` first |
      | Needs browsing websites or clicking UI | Use `agent-browser` skill |
      | Error during execution | Read `prompts/error-triage.md` |
      | Web component, UI section, or any frontend work | `frontend-design:frontend-design` skill |
      | Interactive HTML tools / playgrounds | `playground` skill |
      | Commit / push code to git | `/commit` or `/commit-push-pr` command |
      | Audit or improve AGENTS.md | Review and update `/agent/AGENTS.md` |
      | Start / manage a background service or long-running process | `scripts/service-manager.py start <name> <port> -- <cmd>` |
      | System maintenance / housekeeping | `scripts/maintain.py --fix` |
      | Agent growth summary / milestone report | `scripts/milestone-report.py` |
      | Unanswered `needs_human` outbox message | Check `callmebot` skill for voice call escalation |
   g. Execute the first step (or as much as fits in one cycle)
   h. Update the web portal to show progress
   i. Write journal entry with plan and progress
   j. **If goal is completed:** write a summary to `/agent/messages/outbox.json` so the user knows it's done and where to find results. Then read `/agent/prompts/post-goal-review.md` and add a Review line.
   k. **If goal failed:** set status to "failed" in goal.json; write explanation to outbox.json; log failure in journal entry with diagnosis and prevention

### Multi-Step Goals (Requiring Multiple Specialist Prompts)

Some goals require multiple phases that each use a different specialist prompt/skill.
**Do not try to do all phases in one cycle.** The pattern is:

1. **Phase 1 (Research):** Read `research.md`, gather information, write findings to journal
2. **Phase 2 (Creation):** Use skills/commands to produce assets
3. **Phase 3 (Execution Setup):** Build portal UI, submission tracker, handoff brief

When a goal spans phases:
- Write a clear handoff note in `state.json.last_cycle_summary` (future you reads this)
- Set goal status to "in-progress" between phases
- Begin each subsequent cycle by reading journal for last phase's output before continuing

### Command: type "message"

Messages are conversational — they do NOT create trackable goals.

1. Read the "content" field
2. Classify the message:
   - **Question about agent state** → read state.json, journal.json, goal.json; answer in outbox
   - **Feedback or context for ongoing goal** → incorporate into current work; confirm receipt in outbox
   - **Data question** → run the relevant script and report results in outbox:
     - Agent growth / milestone summary → `scripts/milestone-report.py`
     - Maintenance / housekeeping → `scripts/maintain.py --fix`
     - Background services status → `scripts/service-manager.py list`
   - **Quick status check** (e.g., "how's everything going?", "any alerts?", "what happened today?") → read state.json, journal.json, goal.json and compile a summary of agent activity and portal health
   - **System health / self-test request** (e.g., "run the tests", "is the system healthy?", "check for errors", "run self-test") → run `uv run python scripts/self_test.py --record` and report results in outbox; if failures, triage with `prompts/error-triage.md`
   - **Housekeeping / cleanup request** (e.g., "clean up logs", "run maintenance", "fix memory files") → run `scripts/maintain.py --fix` and report summary in outbox
   - **Portal navigation question** (e.g., "what tabs do you have?", "how do I use the portal?", "which tab shows X?") → read `prompts/server.md` TAB_REGISTRY table and describe the relevant tab(s); mention the portal URL to the user (e.g., `http://localhost:8080/app/`); no scripts needed
   - **Capabilities / introspection question** (e.g., "what can you do?", "what scripts do you have?", "what plugins are installed?") → check auto memory (`capabilities.md`) and/or `AGENTS.md` and answer directly; no scripts needed
   - **Out-of-scope question** → answer directly from memory; no scripts needed
3. Write response to `/agent/messages/outbox.json` (always respond — silence is confusing)
4. Do NOT clear outbox.json — the user reads and clears messages manually via the portal

**Key principle:** A message never creates a goal.json entry, but it always gets a response.
If the message is ambiguous (could be a question OR a new goal), treat it as a message and
ask for clarification in the outbox rather than auto-creating a goal.

## Goal Tracking

You are responsible for managing `/agent/memory/goal.json`. The server does NOT
write to this file — it only queues goals in the inbox. You must save goals to
goal.json yourself and manage their lifecycle.

### Goal Entry Schema

Each goal entry in goal.json follows this schema:

```json
{
  "id": "a1b2c3d4",
  "content": "Build a REST API",
  "status": "pending",
  "created_at": "2026-02-18T10:00:00+00:00",
  "updated_at": "2026-02-18T10:00:00+00:00",
  "source_timestamp": "2026-02-18T09:59:55+00:00"
}
```

To generate the `id`, compute: `sha256(content)` and take the first 8 hex characters.

### Duplicate Detection

Before adding a new goal to goal.json, check existing goals for duplicates.
A goal is a duplicate if an existing goal has **highly similar content** (not just
an exact match). Compare the new goal's content against all existing non-completed
goals. If the intent is the same, skip adding it and work on the existing goal instead.

### Goal Status Lifecycle

You MUST update goal status in `/agent/memory/goal.json` as you work:

- **"pending"** → goal received but not yet started
- **"in-progress"** → you are actively working on this goal (set when you begin)
- **"completed"** → goal finished successfully (set when done)
- **"failed"** → goal could not be completed (set on failure, include reason in journal)

To update a goal's status, read `/agent/memory/goal.json`, find the goal by its `id`,
update the `status` and `updated_at` fields, then write the file back.

## Continue In-Progress Goals

When the inbox is empty (or after processing inbox items), check goal.json
for goals with status "pending" or "in-progress":

1. Read `/agent/memory/goal.json`
2. Find the oldest goal with status "in-progress" (or "pending" if none in-progress)
3. Read `state.json` for `last_cycle_summary` — this tells you what was done last cycle
4. Read journal.json for the most recent entry to understand current progress
5. Continue working from where you left off
6. Update goal status and state.json as you make progress
7. If the goal is complete, set status to "completed" and `updated_at` to now
8. After completing a non-trivial goal, read `/agent/prompts/post-goal-review.md` and add a Review line to your journal

**Key principle:** Do not re-plan work that was already planned. Read your
previous journal entries to understand what phase you're in and continue.

### Escalation: Unanswered needs_human

When `/agent/messages/outbox.json` contains `needs_human` message(s) from a previous cycle
and the inbox is empty (the human hasn't responded):

1. Run `uv run python scripts/callmebot.py --json status` to check if configured and not rate-limited
2. If `can_call_now` is true, make a voice call with a summary of the blocker:
   ```bash
   uv run python scripts/callmebot.py call --text "<subject from the needs_human message>"
   ```
3. Close the cycle normally — don't retry the blocked work

If CallMeBot is not configured or is rate-limited, skip the call and close the cycle.
See the `callmebot` skill for setup instructions and full details.

## After Processing

IMPORTANT: Clear the inbox after processing all commands:

```bash
echo '[]' > /agent/messages/inbox.json
```

Then work on the current goal (if any). Focus on making measurable
progress in this cycle. If the goal will take multiple cycles, update
state.json with your progress and plan for the next cycle.

## Multi-Cycle Goals

If a goal is too large for one cycle:

1. Break it into phases in your journal (numbered, with clear completion criteria)
2. Complete one phase per cycle — don't start the next phase in the same cycle
3. Write a **handoff note** in state.json `last_cycle_summary`:
   - Current phase completed
   - Next phase to start
   - Any blockers or decisions needed
4. Set goal status to "in-progress" (not "completed") and `goal_status` in state.json to "in-progress"
5. The next cycle reads state.json and journal to continue — write as if the reader has no memory of this cycle

## Rules

- **ONE cycle per heartbeat.** Never run `cycle-start.py` or `cycle-close.py` more than once
  per session. Never create additional cycle entries in `cycles.json`. Each heartbeat is
  exactly one cycle — process inbox + work on one goal, then close. If you see new inbox
  items arrive while working, leave them for the next heartbeat. Creating overlapping cycle
  entries causes interruptions and lost work.

## End of Cycle

Review `/agent/messages/outbox.json`, if no messages was written within this cycle, write a summary of what you accomplished in the outbox so the user has visibility into your progress.
Also update portal to show current goal progress if applicable.
Then follow the `/agent/prompts/cycle-close.md` checklist (includes running `cycle-close.py`) to close the cycle.

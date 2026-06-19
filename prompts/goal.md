# GOAL: Process Inbox or Continue In-Progress Goals

> **Enum Reference:** See `prompts/enum.md` for all valid values of `status`, `type`, and other enum fields used in this prompt.

This prompt is triggered when either:

- **New commands** are waiting in `/agent/messages/inbox.json`, OR
- **In-progress goals** exist in `/agent/memory/goal.json` (inbox may be empty)

Important: do **not** re-parse `inbox.json` to discover new goals or messages. They are already parsed and provided in this prompt.

## Cycle Start (run this first)

```bash
uv run python scripts/cycle_start.py --mode goal \
    --goal "<one-line statement of the goal/inbox-task you're taking on this cycle>"
# Omit --goal only if you genuinely don't know yet what you'll work on (rare in goal mode).
```

The `--goal` text is stored on the in-progress cycle entry in `cycles.json` under the
`cycle_goal` field. `cycle_close.py` reads it from there when writing the journal entry,
so you don't need to repeat it at close — pass `--goal` at close only if the plan
diverged or no `cycle_goal` was recorded at start. The goal must describe the *plan*
("Implement X for user request Y", "Continue in-progress goal Z"), not the outcome —
the outcome belongs in `--summary` at close. `state.current_goal` is the dynamic
in-flight sub-task (the agent may update it mid-cycle); it is not what gets written
to the journal. See `prompts/cycle-close.md` for the full rule on goal vs summary.

Review the output before proceeding. If repair-memory-files reports any fixes, note them in your journal.

## Step 0: Determine Mode

Inspect the three pre-extracted sections of this prompt:

- If `<new_goals_to_start>` has items → process them via "New Goals" rules below
- If `<your_inbox_messages>` has items → process them via "Inbox Messages" rules below
- If both `<new_goals_to_start>` and `<your_inbox_messages>` are empty but `<previous_unfinished_goals>` has items → skip to "Continue In-Progress Goals"
- If multiple sections have items → process new goals first, then messages, then continue in-progress goals

## Command Schema

Commands arrive via the `inbox.json` with this schema:

```json
{"type": "<string>", "content": "<string>", "timestamp": "<ISO 8601>"}
```

The system splits inbox commands by type and delivers them in two prompt sections (see `prompts/enum.md` → Message Type):

- **"goal"** — a trackable objective; appears in `<new_goals_to_start>`.
  You are responsible for saving it to `/agent/memory/goal.json` (see Goal Tracking below)
- **"message"** — a conversational message (question, context, feedback, events, etc);
  appears in `<your_inbox_messages>` and is NOT tracked in goal.json

## Processing Rules

### New Goals

For each item in `<new_goals_to_start>`:

1. Read the "content" field — this is your new objective
2. If content starts with "abort", immediately stop any current goal,
   set current_goal to null, status to "idle", and write journal entry
3. If content starts with "status", generate a comprehensive status report:
   Read state.json, journal.json, goal.json and compile a summary of current
   goal progress, blockers, and next steps. Write the report to outbox with
   `type: "error"` if any blockers or errors are present; otherwise
   `type: "info"`.
4. Otherwise, treat it as a new goal:
   a. Check goal.json for duplicates (see Duplicate Detection below)
   b. If not a duplicate, add the goal entry to goal.json with status "pending"
   c. Plan the approach (break into steps if complex)
   d. **Route to specialist prompt if needed** (scan the table, pick the best match):

      | Trigger | Action |
      |---------|--------|
      | Needs learning / unknown API or tool | Read `prompts/research.md` first |
      | Needs browsing websites or clicking UI | Use `agent-browser` skill |
      | Error during execution | Read `prompts/error-triage.md` |
      | Web component, UI section, or any frontend work | `frontend-design:frontend-design` skill |
      | Interactive HTML tools / playgrounds | `playground` skill |
      | Commit / push code to git | `/commit` or `/commit-push-pr` command |
      | Audit or improve AGENTS.md | Review and update `/agent/AGENTS.md` |
      | Start / manage a background service or long-running process | `scripts/service_manager.py start <name> <port> -- <cmd>` |
      | System maintenance / housekeeping | `scripts/maintain.py --fix` |
      | Agent growth summary / milestone report | `scripts/milestone_report.py` |
      | Goal best handled by an online registered agent | Follow "Delegated Goals" — append to delegate's inbox AND record `delegated_to` in goal.json |
   g. Execute all steps (or as much as fits in one cycle)
   h. Update `state.json` -> `current_goal` with the current goal/task in this format: `{goal-id} A short task description no more than 20 words` (concise and short)
   i. Write journal entry with plan and progress
   j. **If goal is completed:** write a summary to `/agent/messages/outbox.json` with `type: "response"` so the user knows it's done and where to find results. Then read `/agent/prompts/post-goal-review.md` and add a Review line.
   k. **If goal failed:** set status to "failed" in goal.json; write explanation to `outbox.json` with `type: "error"` (or `type: "needs_human"` if recovery requires user action); log failure in journal entry with diagnosis and prevention.

### Multi-Step Goals (Requiring Multiple Specialist Prompts)

Some goals require multiple phases that each use a different specialist prompt/skill.
**Do not try to do all phases in one cycle.** The pattern is:

1. **Phase 1 (Research):** Read `research.md`, gather information, write findings to journal
2. **Phase 2 (Creation):** Use skills/commands to produce assets
3. **Phase 3 (Execution Setup):** Build portal UI, submission tracker, handoff brief

When a goal spans phases:

- Write a clear handoff note in `state.json` -> `last_cycle_summary` (future you reads this)
- Set goal status to "in_progress" between phases
- Begin each subsequent cycle by reading journal for last phase's output before continuing

### Delegated Goals

Some goals are best executed by a registered peer agent (internal chat-daemon
agent or external HTTP-API agent). The `[AGENTS]` section of `cycle_start.py`
output lists every online agent with its `name`, `type`, `responsibilities`,
`capabilities`, and `inbox`/`outbox` paths.

Two delegation triggers are valid:

- **Agent-initiated:** you decide to delegate when an online agent's
  responsibilities/capabilities clearly match the goal.
- **User-instructed:** the inbox `goal` content explicitly names a delegate
  (e.g. *"delegate this to research-bot"*). You MUST honour the named target
  if that agent is online; if the named agent is offline or unknown, do NOT
  silently re-route — write a `needs_human` to outbox explaining the issue
  and leave the goal `pending`.

Delegation flow:

1. Generate a UUID4 for the delegated message.
2. Append the message to the delegate's `inbox.json` (path from `[AGENTS]`):
   `{"id":"<uuid>","type":"goal","content":"<task>","timestamp":"<iso>"}`.
   Use `type:"goal"` for assigning new work; use `type:"message"` for a
   conversational status request to an existing in-flight delegated goal.
3. Create the `goal.json` entry as usual (with id, content, created_at,
   etc.) **and** populate the optional fields:
   - `delegated_to: {name, type}` — copy from the agent's registry entry
   - `delegated_at` — current ISO 8601 UTC timestamp
   - `delegated_message_id` — the UUID generated in step 1
   Set initial `status = "in_progress"` (work is in progress from your
   perspective — the wait counts).
4. Update `state.json -> current_goal` to
   `"{goal-id} Delegated to {name}: <short task>"`.
5. Move on. Do NOT block on the delegate's reply within the same cycle.

Replies surface in the main `inbox.json` with `from.source: "external_agent"` or
`from.source: "internal_agent"` and `type` prefixed `agent_` (e.g.
`agent_response`, `agent_error`). The polling step in "Continue In-Progress
Goals" matches these back to your goal via `reply_to_id`:

- For **internal** delegates, the chat daemon preserves the inbound message
  `id` you supplied; when the peer replies via `send_reply(message_id=…)` the
  daemon stamps `reply_to_id: <your-uuid>` on the forwarded envelope.
- For **external** delegates, the agent should pass `reply_to_id: <your-uuid>`
  when calling `POST /write-outbox`; the sweeper copies it onto the
  forwarded main-inbox entry.

Either way, set the goal's `delegated_message_id` to the **same UUID** you
appended to the delegate's inbox so the correlation works.

### Inbox Messages

For each item in `<your_inbox_messages>`:

Messages are conversational — they do NOT create trackable goals.

1. Read the "content" field
2. Classify the message:
   - **Question about agent state** → read state.json, journal.json, goal.json; answer in outbox
   - **Feedback or context for ongoing goal** → incorporate into current work; confirm receipt in outbox
   - **Data question** → run the relevant script and report results in outbox:
     - Agent growth / milestone summary → `scripts/milestone_report.py`
     - Maintenance / housekeeping → `scripts/maintain.py --fix`
     - Background services status → `scripts/service_manager.py list`
   - **Quick status check** (e.g., "how's everything going?", "any alerts?", "what happened today?") → read state.json, journal.json, goal.json and compile a summary of agent activity and portal health
   - **System health / self-test request** (e.g., "run the tests", "is the system healthy?", "check for errors", "run self-test") → run `uv run python scripts/self_test.py --record` and report results in outbox; if failures, triage with `prompts/error-triage.md`
   - **Housekeeping / cleanup request** (e.g., "clean up logs", "run maintenance", "fix memory files") → run `scripts/maintain.py --fix` and report summary in outbox
   - **Portal navigation question** (e.g., "what tabs do you have?", "how do I use the portal?", "which tab shows X?") → read `prompts/server.md` TAB_REGISTRY table and describe the relevant tab(s); mention the portal URL to the user (e.g., `http://localhost:8080/app/`); no scripts needed
   - **Capabilities / introspection question** (e.g., "what can you do?", "what scripts do you have?", "what plugins are installed?") → check `/agent/memory/capabilities.json` and/or `AGENTS.md` and answer directly; no scripts needed
   - **Out-of-scope question** → answer directly from memory; no scripts needed
3. Write response to `/agent/messages/outbox.json` (always respond — silence is confusing).
   To reply to the person who sent the message, copy that inbox message's `id`
   into the outbox `in_reply_to` field — the bridge delivers only to that user
   on their transport. Omit `in_reply_to` only for owner-wide status/FYI. Never
   write raw chat/user ids. See `prompts/enum.md` → Message Envelope.
4. Do NOT clear outbox.json — the user reads and clears messages manually via the portal

**Key principle:** A message never creates a goal.json entry, but it always gets a response.
If the message is ambiguous (could be a question OR a new goal), treat it as a message and
ask for clarification in the outbox rather than auto-creating a goal.

## Goal Tracking

You are responsible for managing `/agent/memory/goal.json`. The system does NOT
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

#### Optional Delegation Fields

When the goal is being executed by a registered peer agent (see "Delegated
Goals" below), add these optional fields. Their **presence** is the signal
that this goal is awaiting another agent rather than being executed locally.

```json
{
  "delegated_to": {"name": "research-bot", "type": "external"},
  "delegated_at": "2026-02-18T10:00:01+00:00",
  "delegated_message_id": "<uuid of the message appended to delegate's inbox>"
}
```

- `delegated_to.type` ∈ {`internal`, `external`} — see `prompts/enum.md` →
  Goal Delegate Type. Must match the `type` field of the matching entry in
  `/agent/memory/agents.json`.
- `delegated_at` is the ISO 8601 UTC timestamp of when the message was placed
  on the delegate's inbox.
- `delegated_message_id` is the `id` of the JSON object you appended to the
  delegate's `inbox.json`; it lets the polling step (see "Continue
  In-Progress Goals") match a forwarded reply back to this goal.

### Duplicate Detection

Before adding a new goal to goal.json, check existing goals for duplicates.
A goal is a duplicate if an existing goal has **highly similar content** (not just
an exact match). Compare the new goal's content against all existing non-completed
goals. If the intent is the same, skip adding it and work on the existing goal instead.

### Goal Status Lifecycle

You MUST update goal status in `/agent/memory/goal.json` as you work (see `prompts/enum.md` → Goal Status):

- **"pending"** → goal received but not yet started
- **"in_progress"** → you are actively working on this goal (set when you begin)
- **"completed"** → goal finished successfully (set when done)
- **"failed"** → goal could not be completed (set on failure, include reason in journal)

To update a goal's status, read `/agent/memory/goal.json`, find the goal by its `id`,
update the `status` and `updated_at` fields, then write the file back.

## Continue In-Progress Goals

After processing `<new_goals_to_start>` and `<your_inbox_messages>` (or if both are empty), work through `<previous_unfinished_goals>`:

#### Step 0: Poll Delegated Goals (run before picking a goal)

For each `in_progress` goal in `goal.json` that has a `delegated_to` field:

1. Look up the delegate by `delegated_to.name` in `/agent/memory/agents.json`.
   - If the agent's `status != "online"` and `last_ping_at` is older than its
     `timeout_seconds` (or older than `delegated_at + timeout_seconds`),
     **auto-mark the goal `failed`**: update `status` and `updated_at`, write
     a journal entry citing the offline/timeout reason, and append a
     `type: "error"` (or `type: "needs_human"` if user action could revive
     the delegate) note to `outbox.json`.
2. Otherwise, scan `<your_inbox_messages>` and recent `inbox_history.json`
   for items where `from.source` is `external_agent`/`internal_agent` AND
   `reply_to_id == <goal.delegated_message_id>`. The sweeper (external) and
   the `send_reply` MCP tool (internal) both stamp `reply_to_id` on the
   forwarded main-inbox envelope — that is the canonical correlation key.
   As a fallback when `reply_to_id` is absent (e.g. a peer agent that never
   replies via `message_id=`), match on the delegate's `from`/`reply_to`
   path plus a content reference.
3. Map the forwarded `type` to a status update:
   - `agent_response` → `status = "completed"`; write outbox `type: "response"`
     summarizing the delivered outcome.
   - `agent_error` → `status = "failed"`; write outbox `type: "error"` with
     the delegate's reason.
   - `agent_needs_human` → leave `in_progress`; the sweeper has already
     mirrored a `needs_human` to the main outbox — just journal the wait.
   - `agent_info` → leave `in_progress`; journal the progress note.
4. On any status change, set `updated_at = now()` and write a journal entry
   citing the delegate.

Then continue with the normal in-progress flow:

1. Pick the oldest goal with status "in_progress" (or "pending" if none in-progress) from `<previous_unfinished_goals>`
2. Read `state.json` -> `last_cycle_summary` — this tells you what was done last cycle
3. Read `journal.json` for the recent entries to understand current progress
4. Continue working from where you left off
5. Update `state.json` -> `agent_status` and `current_goal` as you make progress
6. If the goal is complete, update `/agent/memory/goal.json` to set status to "completed" and `updated_at` to now
7. After completing a non-trivial goal, read `/agent/prompts/post-goal-review.md` and add a Review line to your journal

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

You do **not** need to clear or archive `inbox.json` manually. `scripts/cycle_close.py` (run at the end of every cycle) appends any remaining inbox items to `inbox_history.json` and resets `inbox.json` to `[]` automatically.

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
4. Set the goal's `status` to `"in_progress"` (not `"completed"`) in `goal.json` so the next cycle can resume it.
5. The next cycle reads `state.json` and `journal.json` to continue — write as if the reader has no memory of this cycle.

## Rules

- **ONE cycle per heartbeat.** Never run `cycle_start.py` or `cycle_close.py` more than once
  per session. Never create additional cycle entries in `cycles.json`. Each heartbeat is
  exactly one cycle — process inbox + work on one goal, then close. If you see new inbox
  items arrive while working, leave them for the next heartbeat. Creating overlapping cycle
  entries causes interruptions and lost work.

## End of Cycle

Review `/agent/messages/outbox.json`, if no messages was written within this cycle, write a summary of what you accomplished in the outbox with `type: "response"` (use `type: "needs_human"` if you stopped because of a blocker only a human can unblock e.g — auth, payment, ambiguous decision) so the user has visibility into your progress.
Also update portal to show current goal progress if applicable.

Before running cycle-close, get the current cycle number:
`cycle_n = state.cycle_number + 1` (state stores the *last completed* cycle).
Pass it explicitly: `uv run python scripts/cycle_close.py --cycle <N> --type goal --summary "<delivered outcome>" ...`

Then follow the `/agent/prompts/cycle-close.md` checklist to close the cycle.

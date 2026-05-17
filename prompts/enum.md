# Enum Field Reference

This document defines all enum-type fields used throughout the MewClaw system. These are fields with a fixed set of possible string values.

> **Field-name rename (current schema):** state/cycle/journal records use disambiguated field names so the same concept doesn't collide across files. State entries use `agent_status` (not `status`). Cycle and journal entries use `cycle_number` (not `cycle`), `cycle_status` (not `status`), `cycle_type` (not `type`), `cycle_category` (not `category`); journal entries also use `cycle_goal` (not `goal`). `scripts/memory_repair.py` migrates legacy keys at load time, so on-disk data may still carry the old names until rewritten.

---

## Goal Enums

### Goal Status

**Location:** `/agent/memory/goal.json`

**Field:** `status`

**Description:** Tracks the lifecycle state of a goal.

| Value | Meaning | When Used |
|-------|---------|-----------|
| `pending` | Goal received but not yet started | Initial state when goal is created |
| `in_progress` | Goal actively being worked on | Set when agent begins working on the goal |
| `completed` | Goal finished successfully | Set when goal is accomplished |
| `failed` | Goal could not be completed | Set when goal fails; requires failure reason in journal |

**Notes:**

- Legacy form `in-progress` (with hyphen) is normalized to `in_progress` (with underscore)
- Only one goal should be `in_progress` at a time (excluding goals that are
  delegated to a peer agent — those are also `in_progress` from the main
  agent's perspective but represent a *wait*, not local execution; multiple
  may be outstanding simultaneously)
- See `prompts/goal.md` for goal lifecycle details

### Goal Delegate Type

**Location:** `/agent/memory/goal.json`

**Field:** `delegated_to.type`

**Description:** Identifies the kind of peer agent that a goal has been
delegated to. Present only on goals the main agent has handed off; absent on
locally-executed goals.

| Value      | Meaning                                          | Registered via |
|------------|--------------------------------------------------|----------------|
| `internal` | Internal chat-daemon agent (file-based MCP)      | `scripts/register_internal_agent.py` |
| `external` | External HTTP-API agent (long-running service)   | `scripts/register_external_agent.py` |

**Companion fields on the goal entry** (all optional, set together):

- `delegated_to.name` — the delegate's `name` from `/agent/memory/agents.json`
- `delegated_at` — ISO 8601 UTC timestamp when the message was placed on the
  delegate's inbox
- `delegated_message_id` — UUID of the JSON object appended to the delegate's
  `inbox.json`; used to correlate forwarded replies (`agent_response`,
  `agent_error`, `agent_needs_human`, `agent_info`) back to this goal during
  the polling step.

**Notes:**

- The value MUST match the `type` field of the corresponding entry in
  `/agent/memory/agents.json`.
- Presence of `delegated_to` flips the goal into "polled, not executed" mode —
  see `prompts/goal.md` → Continue In-Progress Goals → Step 0.
- If the delegate goes offline past its `timeout_seconds`, the main agent
  auto-marks the goal `failed` and writes an `error`/`needs_human` to the
  main outbox.

---

## Cycle Enums

### Cycle Status

**Location:** `/agent/memory/cycles.json`, `/agent/memory/journal.json`

**Field:** `cycle_status`

**Description:** Indicates whether a cycle completed successfully or failed.

| Value | Meaning | When Used |
|-------|---------|-----------|
| `completed` | Cycle finished successfully | Normal cycle completion |
| `failed` | Cycle encountered an error or failed to achieve objective | Cycle failed; details in `summary` field |
| `in_progress` | Cycle currently running | Stub written by `cycle_start.py`; promoted to `completed`/`failed` by `cycle_close.py` |
| `interrupted` | Cycle started but never closed | Set by `cycle_start.py::check_orphaned_cycles` (line 238) when an in-progress cycle is older than the max age; an `interrupted_at` ISO timestamp is also recorded |

**Notes:**

- Promoted to `completed`/`failed` by `scripts/cycle_close.py` at end of each cycle
- `interrupted` is the crash-recovery state — written when a previous cycle never reached `cycle_close.py`
- Failures should be documented in journal with root cause

---

### Cycle Type

**Location:** `/agent/memory/cycles.json`, `/agent/memory/journal.json`

**Field:** `cycle_type`

**Description:** Categorizes the purpose of a cycle.

| Value | Meaning | When Used |
|-------|---------|-----------|
| `goal` | Standard goal execution cycle | Processing inbox goals or continuing in-progress goals |
| `evolve` | Self-improvement cycle | No user goals; agent improves itself (see `prompts/evolve.md`) |
| `self-heal` | Recovery/healing cycle | Fixing broken services, corrupt memory, or system errors |
| `dream` | Nightly memory consolidation cycle | Reflecting on transcripts, extracting topics/learnings (see `prompts/dream.md`) |

**Alternate Forms:**

- `self_heal` (with underscore) is an alternate form of `self-heal`

**Notes:**

- Type determines which prompt the agent receives
- `evolve` and `dream` cycles require a `category` field (see below); `goal` and `self-heal` omit it

---

### Evolution Category

**Location:** `/agent/memory/cycles.json`, `/agent/memory/journal.json`

**Field:** `cycle_category`

**Description:** For evolve-type cycles only; specifies the improvement focus area.

| Value | Meaning | When Used | Maturity Penalty |
|-------|---------|-----------|------------------|
| `reliability` | Fix bugs, add error handling, graceful degradation | Portal crashes, script bugs, edge cases | Yes (15 if no recent failures) |
| `observability` | Improve portal visibility, add visualizations, metrics | User can't see agent state, data is confusing | Yes (graduated: 20 at 25+ tabs) |
| `capability` | Build utility scripts, install tools, expand features | Agent can't do something users might ask | Yes (graduated: 20 at 30+ capabilities) |
| `efficiency` | Optimize hot paths, reduce wasted cycles | Slow startup, redundant work, cache misses | Yes (15 if mtime conversion done) |
| `prompt_evolution` | Refine prompts, create new ones, remove outdated instructions | Prompts have stale info or missing guidance | No penalty |
| `memory_consolidation` | Nightly transcript digest — extract topics & learnings | Set by dream cycles when transcripts were processed | No penalty (dream-only; not chosen by evolve recommender) |
| `deep_sleep` | Deep-sleep short-circuit — no transcripts processed | Set by dream cycles that exit at Phase 0 (already in deep sleep for the day) | No penalty (dream-only; not chosen by evolve recommender) |

**Notes:**

- **REQUIRED** for `cycle_type: evolve` and `cycle_type: dream` cycles; omit for `goal` and `self-heal` types
- For `evolve` cycles, `cycle_category` is chosen via evolve recommendation scoring system (see `prompts/evolve.md`)
- For `dream` cycles, `cycle_category` is `memory_consolidation` on a normal run and `deep_sleep` on a deep-sleep short-circuit (see `prompts/dream.md`)
- `memory_consolidation` and `deep_sleep` are produced exclusively by dream cycles — the evolve recommender does not pick them
- Maturity penalties gradually deprioritize well-developed areas
- Used in `app/data/suggest.py` for balance tracking

---

## State Enums

### Agent Status

**Location:** `/agent/memory/state.json`

**Field:** `agent_status`

**Description:** The current operational state of the agent.

| Value | Icon | Meaning | When Used |
|-------|------|---------|-----------|
| `idle` | 🟡 | Waiting for next heartbeat | Between cycles; no active work |
| `running` | 🟢 | Actively executing a cycle | During cycle execution (heartbeat in progress) |
| `healing` | 🔴 | In recovery mode | Self-heal cycle; fixing system errors |
| `bootstrapping` | 🔵 | First-run setup | Initial setup; creating memory files |
| `awaiting_first_heartbeat` | ⚪ | Pre-first cycle | After bootstrap, before first real cycle |
| `waiting_for_human` | 🟠 | Waiting for user input/escalation | Task requires human intervention (see `prompts/goal.md` tasks requiring human help) |

**Notes:**

- Icons displayed in portal header (see `server.py` line 221)
- `waiting_for_human` triggers escalation via CallMeBot if configured
- Most common states: `idle` (between cycles) and `running` (during cycles)

---

## Message Enums

### Message Type (Inbox/Outbox)

**Location:** `/agent/messages/inbox.json`, `/agent/messages/outbox.json`, `/agent/messages/inbox_history.json`

**Field:** `type`

**Description:** Categorizes message/command types for inbox, outbox, and history.

#### Inbox Types

| Value | Meaning | Tracked in goal.json? |
|-------|---------|----------------------|
| `goal` | Trackable objective | Yes |
| `message` | Conversational message (question, feedback) | No |
| `bash` | Bash command execution (legacy/portal) | No |

#### Outbox Types

**Location:** `/agent/messages/outbox.json` (main agent + scripts) and `/agent/messages/external/<name>/outbox.json` (external agents via `POST /external-agent/write-outbox`)

| Value | Meaning | When Used |
|-------|---------|-----------|
| `response` | Standard reply / result for a request | General updates, answers, completed delegated work |
| `needs_human` | Blocked; requires user action | Auth, payment, permissions, ambiguous task (see `system.md` line 122). Highlighted distinctly in the portal; external-agent entries with this type are mirrored into the main outbox so Telegram / WhatsApp / etc. pick them up |
| `error` | Unrecoverable failure | Crash, unexpected exception, irrecoverable state |
| `info` | Unsolicited status / FYI | Heartbeat-style updates and periodic status summaries; lowest priority |

**Notes:**

- The main outbox is **not** schema-validated by `services/shared.py:write_to_outbox` — types above are conventions recognized by display code (`app/commands_tab.py`, `app/shared.py`, `services/telegram_bridge.py`, `services/webhook/whatsapp_bridge_handler.py`). Other types still write, but render without an icon/badge.
- The external-agent endpoint **strictly validates** to `("response", "needs_human", "error", "info")` via `_ALLOWED_OUTBOX_TYPES` in `services/external_agent_api.py:426`.
- Sweeper forward priority (lower = more urgent): `needs_human` (2), `error` (3), `response` (4), `info` (5).
- The sweeper forwards each external-agent outbox entry into the main inbox with the type prefixed `agent_` (`agent_response`, `agent_needs_human`, `agent_error`, `agent_info`) and `source: "external_agent"` — see the next subsection.

**Inbox-side notes:**

- Inbox messages with `type: goal` create persistent goal.json entries
- `type: message` is conversational-only; always gets a response but no goal tracking
- See `prompts/goal.md` for message handling rules

#### External-Agent Inbox Source

Inbox items with `source: "external_agent"` are forwarded by `external_agent_api.py` from a registered external agent's outbox. They carry these extra fields:

- `type`: prefixed with `agent_` — one of `agent_response`, `agent_needs_human`, `agent_error`, `agent_info`. The prefix lets you distinguish forwarded entries from native inbox types (`goal`, `message`, `event`) at a glance; strip the prefix to see the external agent's intent.
- `reply_to_id` (optional): the `id` of the original delegate-inbox message
  this reply addresses. Set when the external agent supplied `reply_to_id`
  on `POST /write-outbox`. The main agent's goal-polling step matches this
  field against `goal.delegated_message_id` to correlate replies back to the
  originating delegated goal. Internal-agent forwards stamp the same
  `reply_to_id` field when the peer replies via
  `mcp__internal_agent_routing__send_reply(message_id=…)`.

#### Internal-Agent Inbox Source

Inbox items with `source: "internal_agent"` are stamped by `services/internal_agent_chat.py` when an internal SDK-hosted peer agent sends a reply via the `mcp__internal_agent_routing__send_reply` tool. They carry the same shape as external-agent forwards:

- `type`: `agent_response` | `agent_needs_human` | `agent_error` | `agent_info` (chosen by the peer agent itself; not auto-prefixed — the peer picks the `agent_*` value directly via the MCP tool — see `services/internal_agent_chat.py:336-339`).
- `source`: literal `"internal_agent"` (stamped by the daemon — `internal_agent_chat.py:402`).
- `reply_to`: path to the peer's own inbox so the main agent can reply back.
- `reply_to_id` (optional): the `id` of the original delegate-inbox envelope being responded to. Same correlation semantics as external — matched against `goal.delegated_message_id`.
- `priority` (optional): integer 1-5 supplied by the peer; defaults to nothing if omitted.
- `agent_needs_human` forwards are mirrored into the main outbox so Telegram / WhatsApp surfaces them, identical to the external-agent path.

---

### Inbox Message Priority

**Location:** `/agent/messages/inbox.json` (and per-agent inboxes under `messages/internal/<name>/`, `messages/external/<name>/`)

**Field:** `priority`

**Description:** Writer-side urgency hint stamped on inbox envelopes by senders (operator CLI, internal-agent reply tool, scheduler). Lower is more urgent.

| Value | Meaning |
|-------|---------|
| `1` | Highest — drop-everything-else urgent |
| `2` | High |
| `3` | Default — used when omitted |
| `4` | Low |
| `5` | Lowest — informational |

---

## Agent Registry Enums

### Agent Control Flag

**Location:** `/agent/memory/agents.json` — entry-level `control` dict (internal agents only)

**Field:** `control.<flag>` → `true`

**Description:** Operator-set control flags consumed by `services/internal_agent_chat.py` on its next sweep. Each flag is a one-shot boolean; the daemon strips the key after applying it (atomic via `_clear_control_keys` — `internal_agent_chat.py:1153`).

| Value | Meaning | When Used |
|-------|---------|-----------|
| `clear_chat` | Archive `memory/chat/<name>/chat_history.json` into `chat_history_archive.json` then truncate, syncing the in-memory chat tail | Operator wants to forget the conversation but keep the SDK session warm |
| `clear_session` | Wipe `memory/chat/<name>/<name>.session` and reconnect the SDK so the next prompt starts fresh | Operator wants a hard SDK reset (e.g. after prompt edits) |

**Set via:**

- `uv run python scripts/interact_with_agent.py clear-chat --name <agent>` (or `--all`)
- `uv run python scripts/interact_with_agent.py clear-session --name <agent>` (or `--all`)

**Notes:**

- Only meaningful for `type: internal` agents — external agents are no-ops.
- Daemon checks for pending flags at sweep start; `sweep_inboxes` returns early when a flag is pending so inbox processing happens after the clear (`internal_agent_chat.py:1297`).
- If clearing fails, the flag stays set so the next sweep retries (critical for `clear_session` reconnect failures).
- Field names are defined as `CONTROL_FIELD = "control"`, `CONTROL_CLEAR_CHAT = "clear_chat"`, `CONTROL_CLEAR_SESSION = "clear_session"` in `services/internal_agent_chat.py:146-148`.

---

## Dream Enums

### Dream Remark Status

**Location:** `/agent/memory/dream/remark.json`

**Field:** `status` (top-level JSON string)

**Description:** Tracks the state of nightly dream (memory consolidation) processing across batches and days.

| Value | Meaning | When Used |
|-------|---------|-----------|
| `light_sleep_dreaming` | A dream stopped mid-window at the 100-page batch limit; more pages remain | Set when `END_PAGE < TOTAL_PAGES`; the `next_page` key MUST be present in the JSON |
| `deep_sleep` | All transcripts in the 24h window for the current `date` are processed | Set when `END_PAGE == TOTAL_PAGES`; the `next_page` key MUST be omitted; subsequent dreams on the same `date` short-circuit |

**Notes:**

- `light_sleep_dreaming` ⇔ `next_page` key present in the JSON
- `deep_sleep` ⇔ `next_page` key absent (omitted entirely, not `null`)

---

## Log Enums

### Bash Log Status

**Location:** `/agent/memory/logs/bash-*.json`

**Field:** `status`

**Description:** Execution state of bash commands logged during cycles.

| Value | Meaning | When Used |
|-------|---------|-----------|
| `running` | Command currently executing | Command started but not finished |
| `exited` | Command has exited | Command finished (check exit code) |
| `completed` | Finished successfully | Exit code 0 |
| `failed` | Command failed | Non-zero exit code |

**Notes:**

- Log files are named `bash-<cycle_number>.json`
- Used in Memory tab for bash command history display

---

## Capability Enums

### Capability Category

**Location:** `/agent/memory/capabilities.json`

**Field:** `category`

**Description:** Groups capabilities by functional area.

| Value | Count | Examples |
|-------|-------|----------|
| `core` | 8 | Bootstrap, heartbeat, Caddy, Streamlit, memory persistence |
| `memory` | 5 | Cycle start/close, memory repair, backup, journal archival |
| `security` | 2 | KeePass credentials, portal auth |
| `communication` | 2 | Chat interface, Telegram bridge |
| `observability` | 7 | Metrics, self-test, health check, app render check, cycle report |
| `automation` | 4 | Task scheduler, service manager, portal hostname, agent browser |
| `portal` | 5 | File upload, command center, memory viewer, system diagnostics, overview |

**Notes:**

- Total 33 capabilities tracked
- Categories align with evolve categories for planning improvements
- Used in `app/data/suggest.py` for capability counting

---

### Capability Enabled

**Location:** `/agent/memory/capabilities.json`

**Field:** `enabled`

**Description:** Whether a capability is currently active.

| Value | Meaning | Examples |
|-------|---------|----------|
| `true` | Capability is enabled and active | Most capabilities (31/33) |
| `false` | Capability is disabled or optional | `portal_auth`, `telegram_bridge` |

**Notes:**

- Disabled capabilities: `portal_auth` (optional security), `telegram_bridge` (requires setup)
- Disabled capabilities can be enabled by user configuration

---

## Portal Enums

### Suggestion Priority

**Location:** Portal suggestions (generated by `app/data/suggest.py`)

**Field:** `priority`

**Description:** Urgency level of action suggestions shown in Overview tab.

| Value | Meaning | Color/Icon |
|-------|---------|------------|
| `high` | Urgent; should be addressed soon | Red/⚠️ |
| `medium` | Important but not urgent | Yellow/ℹ️ |
| `low` | Nice to have; low urgency | Gray/💡 |

**Notes:**

- High priority: unfinished goals, recent failures, stale backups
- Medium priority: evolve suggestions, efficiency opportunities
- Low priority: general improvements, documentation updates

---

### Suggestion Category

**Location:** Portal suggestions (generated by `app/data/suggest.py`)

**Field:** `category`

**Description:** Type of suggestion for filtering and grouping.

| Value | Meaning | When Shown |
|-------|---------|-----------|
| `goal` | Goal-related suggestion | Unfinished goals exist |
| `reliability` | System reliability issue | Recent failures or errors |
| `efficiency` | Performance/efficiency issue | Slow cycles, redundant work |
| `evolve` | Evolution recommendation | Time for self-improvement |

**Notes:**

- Used for color-coding and filtering in Overview tab
- Aligns with evolve categories for consistency

---

### Activity Event Type

**Location:** Activity feed (generated by `app/data/cycle.py`)

**Field:** `type`

**Description:** Type of activity event shown in portal activity timeline.

| Value | Display | Source |
|-------|---------|--------|
| `goal` | 🎯 Goal | Command history `type: goal` |
| `message` | 💬 Message | Command history `type: message` |
| `bash_cmd` | 💻 Command | Command history `type: bash` or `message` |
| `cycle_start` | 🔄 Cycle Start | Cycle start events |
| `cycle_end` | ✅ Cycle End | Cycle completion events |

**Notes:**

- `message` and `bash` from history normalize to `bash_cmd` in activity feed
- Used in Memory tab activity timeline

---

## Skill-Creator Enums

### Grading Result

**Location:** `skills/skill-creator/` (history.json, grading.json)

**Field:** `grading_result`

**Description:** Outcome of skill version comparison in skill-creator improve mode.

| Value | Meaning |
|-------|---------|
| `baseline` | Initial version (v0); no comparison yet |
| `won` | New version outperformed previous version |
| `lost` | New version underperformed previous version |
| `tie` | New version performed equally to previous version |

**Notes:**

- Used by skill-creator to track skill evolution
- See `skills/skill-creator/references/schemas.md` for full schema

---

## Usage Guidelines

### When Adding New Enums

1. **Document here first** — add to this file before implementing
2. **Use snake_case** — prefer `in_progress` over `in-progress` (easier for Python)
3. **Normalize legacy forms** — if changing formats, support old values during transition
4. **Validate on load** — add validation in scripts/app code to catch typos
5. **Update prompts** — ensure prompts reference the correct enum values

### When Changing Enums

1. **Check all usages** — grep for the field name across codebase
2. **Support migration** — normalize old values to new ones (e.g., `in-progress` → `in_progress`)
3. **Update this doc** — keep enum.md in sync with code
4. **Update prompts** — fix any prompt files that reference old values
5. **Test portal display** — ensure UI handles new values correctly

---

## Quick Reference Table

| Field | Context | Values | File(s) |
|-------|---------|--------|---------|
| `status` | Goal | `pending`, `in_progress`, `completed`, `failed` | `goal.json` |
| `delegated_to.type` | Goal | `internal`, `external` | `goal.json` (optional) |
| `cycle_status` | Cycle | `completed`, `failed`, `in_progress`, `interrupted` | `cycles.json`, `journal.json` |
| `agent_status` | Agent | `idle`, `running`, `healing`, `bootstrapping`, `awaiting_first_heartbeat`, `waiting_for_human` | `state.json` |
| `status` | Bash Log | `running`, `exited`, `completed`, `failed` | `logs/bash-*.json` |
| `status` | Dream Remark | `light_sleep_dreaming`, `deep_sleep` | `dream/remark.json` |
| `cycle_type` | Cycle | `goal`, `evolve`, `self-heal`, `dream` | `cycles.json`, `journal.json` |
| `type` | Inbox | `goal`, `message`, `bash` | `inbox.json`, `inbox_history.json` |
| `type` | Outbox | `response`, `needs_human`, `error`, `info` | `outbox.json`, `messages/external/<name>/outbox.json` |
| `type` | Forwarded Inbox (from external agent) | `agent_response`, `agent_needs_human`, `agent_error`, `agent_info` | `inbox.json` (with `source: "external_agent"`) |
| `type` | Forwarded Inbox (from internal agent) | `agent_response`, `agent_needs_human`, `agent_error`, `agent_info` | `inbox.json` (with `source: "internal_agent"`) |
| `priority` | Inbox envelope | `1`, `2`, `3` (default), `4`, `5` | `inbox.json`, `messages/internal/<name>/inbox.json`, `messages/external/<name>/inbox.json` |
| `control.<flag>` | Agent Registry | `clear_chat`, `clear_session` (one-shot booleans on internal agents) | `agents.json` |
| `cycle_category` | Evolution | `reliability`, `observability`, `capability`, `efficiency`, `prompt_evolution`, `memory_consolidation`, `deep_sleep` | `cycles.json`, `journal.json` |
| `category` | Capability | `core`, `memory`, `security`, `communication`, `observability`, `automation`, `portal` | `capabilities.json` |
| `enabled` | Capability | `true`, `false` | `capabilities.json` |
| `priority` | Suggestion | `high`, `medium`, `low` | Portal suggestions |
| `grading_result` | Skill-Creator | `baseline`, `won`, `lost`, `tie` | `skills/skill-creator/` |

---

## Related Documentation

- **Goal lifecycle:** `prompts/goal.md`
- **Cycle close:** `prompts/cycle-close.md`
- **Evolution categories:** `prompts/evolve.md`
- **Dream lifecycle:** `prompts/dream.md`
- **State management:** `prompts/bootstrap.md`
- **Capabilities list:** `memory/capabilities.json`
- **Skill-creator schemas:** `skills/skill-creator/references/schemas.md`

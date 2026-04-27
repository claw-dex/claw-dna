# Enum Field Reference

This document defines all enum-type fields used throughout the MewClaw system. These are fields with a fixed set of possible string values.

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
- Only one goal should be `in_progress` at a time
- See `prompts/goal.md` for goal lifecycle details

---

## Cycle Enums

### Cycle Status

**Location:** `/agent/memory/cycles.json`, `/agent/memory/journal.json`

**Field:** `status`

**Description:** Indicates whether a cycle completed successfully or failed.

| Value | Meaning | When Used |
|-------|---------|-----------|
| `completed` | Cycle finished successfully | Normal cycle completion |
| `failed` | Cycle encountered an error or failed to achieve objective | Cycle failed; details in `summary` field |
| `in_progress` | Cycle currently running | Used in journal entries during execution |

**Notes:**

- Set by `scripts/cycle_close.py` at end of each cycle
- Failures should be documented in journal with root cause

---

### Cycle Type

**Location:** `/agent/memory/cycles.json`, `/agent/memory/journal.json`

**Field:** `type`

**Description:** Categorizes the purpose of a cycle.

| Value | Meaning | When Used |
|-------|---------|-----------|
| `goal` | Standard goal execution cycle | Processing inbox goals or continuing in-progress goals |
| `evolve` | Self-improvement cycle | No user goals; agent improves itself (see `prompts/evolve.md`) |
| `self-heal` | Recovery/healing cycle | Fixing broken services, corrupt memory, or system errors |

**Alternate Forms:**

- `self_heal` (with underscore) is an alternate form of `self-heal`

**Notes:**

- Type determines which prompt the agent receives
- Evolve cycles require a `category` field (see below)

---

### Evolution Category

**Location:** `/agent/memory/cycles.json`, `/agent/memory/journal.json`

**Field:** `category`

**Description:** For evolve-type cycles only; specifies the improvement focus area.

| Value | Meaning | When Used | Maturity Penalty |
|-------|---------|-----------|------------------|
| `reliability` | Fix bugs, add error handling, graceful degradation | Portal crashes, script bugs, edge cases | Yes (15 if no recent failures) |
| `observability` | Improve portal visibility, add visualizations, metrics | User can't see agent state, data is confusing | Yes (graduated: 20 at 25+ tabs) |
| `capability` | Build utility scripts, install tools, expand features | Agent can't do something users might ask | Yes (graduated: 20 at 30+ capabilities) |
| `efficiency` | Optimize hot paths, reduce wasted cycles | Slow startup, redundant work, cache misses | Yes (15 if mtime conversion done) |
| `prompt_evolution` | Refine prompts, create new ones, remove outdated instructions | Prompts have stale info or missing guidance | No penalty |

**Notes:**

- **REQUIRED** for `type: evolve` cycles; omit for `goal` and `self-heal` types
- Category is chosen via evolve recommendation scoring system (see `prompts/evolve.md`)
- Maturity penalties gradually deprioritize well-developed areas
- Used in `app/data/suggest.py` for balance tracking

---

## State Enums

### Agent Status

**Location:** `/agent/memory/state.json`

**Field:** `status`

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

**Location:** `/agent/messages/inbox.json`, `/agent/messages/outbox.json`, `/agent/memory/command_history.json`

**Field:** `type`

**Description:** Categorizes message/command types for inbox, outbox, and history.

#### Inbox Types

| Value | Meaning | Tracked in goal.json? |
|-------|---------|----------------------|
| `goal` | Trackable objective | Yes |
| `message` | Conversational message (question, feedback) | No |
| `bash` | Bash command execution (legacy/portal) | No |

#### Outbox Types

| Value | Meaning | When Used |
|-------|---------|-----------|
| `response` | Standard agent response | General updates, answers |
| `needs_human` | Requires user action | Blocked on auth, payment, permissions (see `system.md` line 122) |
| `goal_complete` | Goal completion notification | When goal status → `completed` |
| `goal_failed` | Goal failure notification | When goal status → `failed` |

**Notes:**

- Inbox messages with `type: goal` create persistent goal.json entries
- `type: message` is conversational-only; always gets a response but no goal tracking
- Outbox `needs_human` messages are highlighted distinctly in the portal
- See `prompts/goal.md` for message handling rules

---

## Dream Enums

### Dream Remark Status

**Location:** `/agent/memory/dream/remark.md`

**Field:** `Status:` (Markdown front-matter style line)

**Description:** Tracks the state of nightly dream (memory consolidation) processing across batches and days.

| Value | Meaning | When Used |
|-------|---------|-----------|
| `light_sleep_dreaming` | A dream stopped mid-window at the 100-page batch limit; more pages remain | Set when `END_PAGE < TOTAL_PAGES`; remark must include `Next page:` and **omit** the trailing "deep sleep" line |
| `deep_sleep` | All transcripts in the 24h window for the current `Date:` are processed | Set when `END_PAGE == TOTAL_PAGES`; remark must include the trailing "All transcripts for date … are now completed. You are in deep sleep." line; subsequent dreams on the same `Date:` short-circuit |

**Notes:**

- `light_sleep_dreaming` ⇔ `Next page:` line present and trailing deep-sleep line absent
- `deep_sleep` ⇔ no `Next page:` line and trailing deep-sleep line present

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

**Description:** Urgency level of action suggestions shown in Agent Overview tab.

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

- Used for color-coding and filtering in Agent Overview tab
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

### Search Result Source

**Location:** Portal search results (generated by `app/data/search.py`)

**Field:** `source`

**Description:** Origin of a search result for display and filtering.

| Value | Searches In | Display Badge |
|-------|------------|---------------|
| `journal` | `/agent/memory/journal.json` | 📓 Journal |
| `goal` | `/agent/memory/goal.json` | 🎯 Goal |
| `cycle` | `/agent/memory/cycles.json` | 🔄 Cycle |
| `history` | `/agent/memory/command_history.json` | 📜 History |

**Notes:**

- Used in Memory tab search feature
- Results grouped and color-coded by source

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
| `status` | Cycle | `completed`, `failed`, `in_progress` | `cycles.json`, `journal.json` |
| `status` | Agent | `idle`, `running`, `healing`, `bootstrapping`, `awaiting_first_heartbeat`, `waiting_for_human` | `state.json` |
| `status` | Bash Log | `running`, `exited`, `completed`, `failed` | `logs/bash-*.json` |
| `Status` | Dream Remark | `light_sleep_dreaming`, `deep_sleep`, `completed` (legacy) | `dream/remark.md` |
| `type` | Cycle | `goal`, `evolve`, `self-heal` | `cycles.json`, `journal.json` |
| `type` | Message | `goal`, `message`, `bash`, `needs_human`, `response`, `goal_complete`, `goal_failed` | `inbox.json`, `outbox.json`, `command_history.json` |
| `category` | Evolution | `reliability`, `observability`, `capability`, `efficiency`, `prompt_evolution` | `cycles.json`, `journal.json` |
| `category` | Capability | `core`, `memory`, `security`, `communication`, `observability`, `automation`, `portal` | `capabilities.json` |
| `enabled` | Capability | `true`, `false` | `capabilities.json` |
| `priority` | Suggestion | `high`, `medium`, `low` | Portal suggestions |
| `source` | Search | `journal`, `goal`, `cycle`, `history` | Portal search |
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

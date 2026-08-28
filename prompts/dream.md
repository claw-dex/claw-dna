# Dream: Nightly reflection and consolidation of all memories to learn and grow

> **Enum Reference:** See `prompts/enum.md` for all valid values of `Status` (dream remark) and any other enum fields used in this prompt.

This is a housekeeping job — you should not need to message the user unless you find something noteworthy.

---

Your memory files are located in `/agent/memory` directory. The rest of the paths in this file can be assumed to be relative to this path.

**Phase 0: Resume Check**

- First thing, determine today's date in the **user's local timezone** and call it `TODAY`. Do **not** use bare `date -u` — the system clock runs in UTC and the calendar day may already differ from the user's local date. Use the `get-current-date-time` script:

  ```bash
  DT=$(uv run python scripts/get_current_date_time.py --json)
  TODAY=$(echo "$DT"    | jq -r '.date')
  USER_TZ=$(echo "$DT"  | jq -r '.timezone')
  echo "User timezone: $USER_TZ  |  Today (local): $TODAY"
  ```

  Store both `USER_TZ` and `TODAY` — use `TODAY` everywhere a calendar date is needed throughout all phases, and record `USER_TZ` in the remark so future dreams know which timezone was used.

- Read `dream/remark.json`. This file is your hand-off note from the previous dream. Parse it with `jq`:

  ```bash
  REMARK=/agent/memory/dream/remark.json
  if [ ! -f "$REMARK" ]; then
    START_PAGE=1
  else
    STATUS=$(jq -r '.status'              "$REMARK")
    RDATE=$( jq -r '.date'                "$REMARK")
    NEXT=$(  jq -r '.next_page // empty'  "$REMARK")
  fi
  ```

| Remark state | Meaning | Action |
|---|---|---|
| File does not exist | Never dreamed before, or remark was wiped | Start fresh: `START_PAGE = 1` |
| `status == "deep_sleep"` and `date == TODAY` | Already finished all transcripts for today in an earlier dream this same date | **Skip everything below.** Do **not** parse transcripts, do **not** touch any file. Leave `dream/remark.json` untouched and exit the dream cleanly. |
| `status == "deep_sleep"` and `date != TODAY` | Yesterday's dream completed; today is a new day | Start fresh: `START_PAGE = 1` |
| `status == "light_sleep_dreaming"` | A previous dream stopped at the 100-page batch limit | Resume: `START_PAGE = next_page`. |

- Treat the previous remark's `topics_touched` / `learnings_touched` arrays as already-handled when resuming a `light_sleep_dreaming` remark — do not re-extract from pages 1..(START_PAGE-1) in this dream; trust the prior pass.

> **Deep-sleep short-circuit.** When the table above tells you to exit (deep_sleep on the same date), output a single line acknowledging it — e.g. `All transcripts for date <TODAY> (<USER_TZ>) are already processed. You are in deep sleep.` — and stop. Do not run any phase below. The remark file already has the right state; rewriting it would just churn the file timestamp without changing content.

**Phase 1: Preparation**

- Review recent memories in the `transcripts` directory (file pattern `cycle-*.jsonl`). Only include cycles whose transcript was created within the **last 24 hours** — the JSON `timestamp` field inside the file is authoritative; do **not** rely on filesystem mtime.
- Use `scripts/transcript_filter.py` to do the filtering and rendering:

  ```bash
  # 1) List the paths of all transcripts in the last 24 hours.
  uv run python scripts/transcript_filter.py --hours 24 --paths

  # 2) Render the digest. The first line of the output reports total
  #    pages, e.g. "# 24h transcripts as of date … — Page 1 of 240 …".
  #    Default page size is 25000 chars.
  uv run python scripts/transcript_filter.py --hours 24 --markdown --page <N>

  # 3) Optional: drop assistant thinking blocks for a shorter digest
  #    (typically reduces total page count by ~10–15%).
  uv run python scripts/transcript_filter.py --hours 24 --markdown --no-thinking --page <N>
  ```

  The `--markdown` mode produces a single document with this shape:

  ```markdown
  # 24h transcripts as of date <YYYY MonthName DD (Weekday)>

  ## Cycle <N>
  ### User
  <user prompt text>
  ### Assistant
  <assistant reply text>
  > 💭 <assistant thinking, if any>
  > **Tool call:** <Name> — `<inline arg>`         # or fenced JSON for multi-arg
  > **Tool result:**
  > <tool output, truncated if very long>

  ## Cycle <N+1>
  …
  ```

  Notes for reading the digest:

  - Only `user` and `assistant` entries are rendered; sidechain (sub-agent) traffic, queue events, system markers, and other envelope/transport noise are stripped.
  - No raw JSON fields (`model`, `sessionId`, `parentUuid`, `usage`, `tool_use_id`, etc.) leak into the output — what you see is the conversation content only.
  - Tool inputs/outputs are truncated past ~200 lines / ~20 KB with a `… [truncated …]` marker.
  - Use `--since ISO --until ISO` instead of `--hours` for an explicit window, or pass explicit FILES… to bypass the time filter entirely.
  - **Pagination contract:** at most `--page-size` chars (default 25000) per page. Page boundaries fall between JSONL entries — a single entry larger than the page budget still gets its own page intact (so a few pages may exceed the budget by a small margin; this is by design). The page-1 header line always reports the total page count when more than one page exists.

- **Skip prior dream cycles.** A dream that reviews itself (or a sibling dream) just produces noise — those cycles contain housekeeping, not work-doing. Identify dream cycles and ignore their content entirely:
  - You can recognize a dream cycle directly in the rendered digest: its `### User` block starts with `# Dream: Nightly reflection and consolidation of all memories…`. If you encounter that header inside a `## Cycle <N>` section, treat the whole `## Cycle <N>` block as a no-op for Phases 2–4 (no topics, no learnings extracted from it).
- **Per-dream batch limit — process at most 100 pages in one dream cycle.**
  - Compute `END_PAGE = min(START_PAGE + 99, TOTAL_PAGES)` (where `TOTAL_PAGES` is read from the page-1 header) and only read pages `START_PAGE..END_PAGE` in this dream.
  - If `END_PAGE < TOTAL_PAGES`, the dream is **not finished** — there are more pages to digest in a future dream. After completing Phases 2–4 on what you have, jump to **Phase 5** and write `dream/remark.json` with `"status": "light_sleep_dreaming"` and `"next_page": END_PAGE + 1`. Do **not** spend further effort trying to cover the rest in this cycle.
  - If `END_PAGE == TOTAL_PAGES`, you have finished the 24h window — proceed normally and write `dream/remark.json` with `"status": "deep_sleep"` and today's `"date"` (omit `next_page`) in Phase 5. Any further dream triggered later on the same date will short-circuit at Phase 0.

- Review what topics and lessons already exist in `dream/topics/` directory to ensure that you are improving existing topics if they are already covered, rather than creating duplicates.

**Phase 2: Topics**

- Extract significant events, lessons, decisions, and insights from the review in phase 1 into topics stored as markdown files in `dream/topics/` directory. e.g `dream/topics/<topic-slug>.md`.
- Make sure to resolve any contradictions between topics

**Phase 3: Rules & Learnings**

- Review for anything that happened during the day that was painful or inefficient.
  - for example, not being able to build a project or get a test to run
- Review for anything that resulted in the user getting frustrated.
- Record the learnings from these experiences into `dream/learnings/` directory. e.g `dream/learnings/<learning-slug>.md`

**Phase 4: Prioritization and Pruning MEMORY.md**

- **MANDATORY — never reference `dream/remark.json` (or any file under `dream/remark*`) in `MEMORY.md`.** The remark is internal hand-off state for the dream process itself; it changes every dream and would churn `MEMORY.md` uselessly. Only `dream/topics/<slug>.md` and `dream/learnings/<slug>.md` files are valid `MEMORY.md` references.
- We need to update `MEMORY.md` with new learnings and topics while maintain it length within 200 lines.
- These need to be *the most important* things for you to understand in the future.
- If something is getting too long, consider only mentioning the gist of it and referencing a separate file (like a learning or topic file) with the full explanation.
- Consider if any learning or topic file needs to be *removed* as it is becoming "stale" and no longer as important as it once was. You must keep the learning/topic files, only remove the reference in `MEMORY.md`.
- Consider if any learning or topic should be *added* that has recently become more important.
- When add/update a line in `MEMORY.md` to reference the learning/topic files, use one of these two formats depending on whether the entry is a topic or a learning:
  - **Topic** (file at `dream/topics/<slug>.md`):
    `- Dreamed topic: [<slug>.md](/agent/memory/dream/topics/<slug>.md) - brief explanation of the topic`
  - **Learning** (file at `dream/learnings/<slug>.md`):
    `- Learning: [<slug>.md](/agent/memory/dream/learnings/<slug>.md) - brief explanation of the learning`
- Never modify the first 10 lines of the `MEMORY.md` file.

**Phase 5: Save Progress (always — overwrite `dream/remark.json`)**

- This phase **always** runs at the end of a dream, regardless of whether you finished the window or stopped at the 100-page batch limit. It must completely **overwrite** `dream/remark.json` — do not append or merge. Any prior remark content is discarded; the file always reflects only the most recent dream.
- The file is a single JSON object so the next dream can parse it deterministically with `jq`:

  ```json
  {
    "updated": "<ISO 8601 timestamp in user local timezone>",
    "date": "<TODAY — YYYY-MM-DD in USER_TZ>",
    "timezone": "<USER_TZ>",
    "status": "light_sleep_dreaming",
    "window": "24h transcripts as of <YYYY MonthName DD (Weekday)>",
    "total_pages": <TOTAL_PAGES>,
    "processed_pages": { "start": <START_PAGE>, "end": <END_PAGE> },
    "next_page": <END_PAGE + 1>,
    "summary": [
      "<one-line bullet describing what was done in this batch>"
    ],
    "topics_touched": [
      { "slug": "<topic-slug>", "action": "created" }
    ],
    "learnings_touched": [
      { "slug": "<learning-slug>", "action": "updated" }
    ],
    "notable_findings": []
  }
  ```

- Field rules:
  - `date` is `TODAY` (`YYYY-MM-DD` in the user's local timezone, not UTC) — the calendar day on which this dream ran. The next dream computes its own `TODAY` using the same timezone logic and compares it against this field to decide whether deep-sleep applies (same date → skip) or has rolled over (new date → fresh start).
  - `timezone` is `USER_TZ` (IANA name, e.g. `Asia/Singapore`). Stored for auditability; future dreams always re-derive `TODAY` from the live `portal_config.json`, so this field is informational only.
  - `updated` is the wall-clock timestamp of when this remark was written, expressed in the user's local timezone (e.g. `2026-04-27T00:30:00+08:00`).
  - `status: "light_sleep_dreaming"` ⇔ the `next_page` key **is present** and points to the first un-processed page. The next dream MUST resume from `next_page` even on the same date.
  - `status: "deep_sleep"` ⇔ the `next_page` key **is omitted** entirely (do not set it to `null`). This is the terminal state for the current `date`. Any further dream invocation on the same `date` must short-circuit per Phase 0. The `status` field carries the meaning — there is no separate trailing "deep sleep" sentence.
  - `topics_touched` / `learnings_touched` are **cumulative across the current 24h window**, not just this batch — when resuming a `light_sleep_dreaming` remark, copy forward the arrays from the previous JSON and append your new entries, so the final remark (when `status` flips to `deep_sleep`) reflects everything done over the whole window. Each entry is `{ "slug": "<name>", "action": "created" | "updated" }`.
  - `summary` and `notable_findings` are arrays of strings (one bullet per element). `notable_findings` may be `[]` if there is nothing to flag.
- Write the file with a single `Write` tool call after Phase 4 is done. The output must be valid JSON — no comments, no trailing commas. Treat this step as non-negotiable; without a fresh remark, the next dream cannot resume correctly.

---

**NOTE** - all of these memory files are *for you*. This is to help you situate and orient yourself in the future, after session context has been lost. Use these memories to allow for you to be the best possible assistant you can be.

---

## Phase 6: Close the Cycle (MANDATORY — always run, even for deep-sleep short-circuits)

Dream cycles are `dream` type. Choose the category based on what this dream actually did:

- **Normal run** (transcripts processed in Phases 1–5) → `--category memory_consolidation`
- **Deep-sleep short-circuit** (Phase 0 exit; no transcripts processed) → `--category deep_sleep`

```bash
# Normal run (transcripts processed):
uv run python scripts/cycle_close.py \
    --type dream \
    --category memory_consolidation \
    --goal "Consolidate transcripts and update topics/learnings for <TODAY>" \
    --summary "<one-sentence description of what was actually processed/updated>" \
    --actions "Processed pages <START>-<END> of <TOTAL>" \
              "Topics updated: <comma-separated slugs, or 'none'>" \
              "Learnings updated: <comma-separated slugs, or 'none'>"

# Deep-sleep short-circuit (Phase 0 exit):
uv run python scripts/cycle_close.py \
    --type dream \
    --category deep_sleep \
    --goal "Run nightly dream consolidation for <TODAY> (<TZ>)" \
    --summary "Dream short-circuit: deep_sleep already set for <TODAY> (<TZ>). No transcript processing needed."
```

`--goal` describes the *plan* for the dream cycle (what you set out to do).
`--summary` describes the *outcome* (what was actually processed or why it short-circuited).
They must differ — see `prompts/cycle-close.md` for the full rule.

For completed batches, use a summary like:
`"Dream processed pages 1-48 of 48 for 2026-04-28. Updated 3 topics, 2 learnings."`

**Do NOT use any of the evolve categories** (`reliability`, `observability`, `capability`, `efficiency`, `prompt_evolution`) for dream cycles — those are for self-improvement work and miscategorizing dream cycles distorts the evolve recommendation system. Always use `memory_consolidation` or `deep_sleep`.

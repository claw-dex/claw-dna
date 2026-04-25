# Dream: Nightly reflection and consolidation of all memories to learn and grow

This is a housekeeping job — you should not need to message the user unless you find something noteworthy.

---

Your memory files are located in `/agent/memory` directory. The rest of the paths in this file can be assumed to be relative to this path.

**Phase 0: Resume Check**

- Read `dream/remark.md` first thing. This file is your hand-off note from the previous dream.
  - If the file does **not** exist, or its `Status:` is `completed`, you are starting a fresh dream — set `START_PAGE = 1`.
  - If `Status:` is `in_progress`, read the `Next page:` line and set `START_PAGE` to that value. The previous dream stopped because the 24h window had more than 100 pages of transcripts to digest, and it asked you to pick up where it left off.
- Treat the previous remark's `Topics touched:` / `Learnings touched:` lists as already-handled — do not re-extract from pages 1..(START_PAGE-1) in this dream; trust the prior pass.

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
  - The authoritative source is `/agent/memory/cycles.json` — any entry whose `type == "dream"` is a dream cycle. Build a set of those `cycle` numbers and skip transcripts named `cycle-<N>.jsonl` for any `N` in that set.
  - As a fallback, you can recognize a dream cycle directly in the rendered digest: its `### User` block starts with `# Dream: Nightly reflection and consolidation of all memories…`. If you encounter that header inside a `## Cycle <N>` section, treat the whole `## Cycle <N>` block as a no-op for Phases 2–4 (no topics, no learnings extracted from it).
  - Do **not** count skipped dream cycles against the 100-page batch budget — they don't add to topics/learnings, so reading past them is essentially free.

- **Per-dream batch limit — process at most 100 pages in one dream cycle.**
  - Compute `END_PAGE = min(START_PAGE + 99, TOTAL_PAGES)` (where `TOTAL_PAGES` is read from the page-1 header) and only read pages `START_PAGE..END_PAGE` in this dream.
  - If `END_PAGE < TOTAL_PAGES`, the dream is **not finished** — there are more pages to digest in a future dream. After completing Phases 2–4 on what you have, jump to **Phase 5** and write `Status: in_progress` to `dream/remark.md` with `Next page: END_PAGE + 1`. Do **not** spend further effort trying to cover the rest in this cycle.
  - If `END_PAGE == TOTAL_PAGES`, you have finished the 24h window — proceed normally and write `Status: completed` to `dream/remark.md` in Phase 5.

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

- We need to update `MEMORY.md` with new learnings and topics while maintain it length within 200 lines.
- These need to be *the most important* things for you to understand in the future.
- If something is getting too long, consider only mentioning the gist of it and referencing a separate file (like a learning or topic file) with the full explanation.
- Consider if any learning or topic file needs to be *removed* as it is becoming "stale" and no longer as important as it once was. You must keep the learning/topic files, only remove the reference in `MEMORY.md`.
- Consider if any learning or topic should be *added* that has recently become more important.
- When add/update a line in `MEMORY.md` to reference the learning/topic files, follow this format: `- [learning-or-topic-slug.md](/agent/memory/learnings/earning-or-topic-slug.md) - brief explanation of the learning/topic`
- Never modify the first 10 lines of the `MEMORY.md` file.

**Phase 5: Save Progress (always — overwrite `dream/remark.md`)**

- This phase **always** runs at the end of a dream, regardless of whether you finished the window or stopped at the 100-page batch limit. It must completely **overwrite** `dream/remark.md` — do not append. Any prior remark content is discarded; the file always reflects only the most recent dream.
- The file format is fixed Markdown so the next dream can parse it deterministically:

  ```markdown
  # Dream remark
  Updated: <ISO 8601 UTC timestamp>
  Status: in_progress | completed
  Window: 24h transcripts as of <YYYY MonthName DD (Weekday)>
  Total pages: <TOTAL_PAGES>
  Processed pages: <START_PAGE>-<END_PAGE>
  Next page: <END_PAGE + 1>          # omit this line when Status: completed

  ## Summary
  - <one-line bullet describing what was done in this batch>
  - <…>

  ## Topics touched
  - <topic-slug.md> — created | updated
  - <…>

  ## Learnings touched
  - <learning-slug.md> — created | updated
  - <…>

  ## Notable findings
  - <anything noteworthy that the next dream — or you in a future cycle — should be aware of; leave the section empty if nothing>
  ```

- Field rules:
  - `Status: in_progress` ⇔ `Next page:` line is present and points to the first un-processed page. The next dream MUST resume from that page.
  - `Status: completed` ⇔ no `Next page:` line. The next dream starts fresh at page 1 over a new 24h window.
  - `Topics touched` / `Learnings touched` are **cumulative across the current 24h window**, not just this batch — when resuming an in_progress dream, copy forward the lists from the previous remark and append your new entries, so the final remark (when Status flips to completed) reflects everything done over the whole window.
- Write the file with a single `Write` tool call after Phase 4 is done. Treat this step as non-negotiable; without a fresh remark, the next dream cannot resume correctly.

---

**NOTE** - all of these memory files are *for you*. This is to help you situate and orient yourself in the future, after session context has been lost. Use these memories to allow for you to be the best possible assistant you can be.

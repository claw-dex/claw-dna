# RESEARCH: Structured Investigation Before Acting

Use this prompt when a goal requires understanding something you don't already
know before you can plan or implement. Signs you need this prompt:

- The goal mentions a technology, API, or tool you haven't used
- You need current information (prices, docs, status of external services)
- The goal is vague and you need to explore options before committing
- Previous attempts failed due to incorrect assumptions

## Research Protocol

### 1. Define the Question (before any searching)

Write down exactly what you need to learn. Be specific:
- BAD: "Research React" — too broad, wastes cycles
- GOOD: "What's the recommended way to add SSR to an existing React 18 SPA?"

If the goal is vague, break it into 2-3 specific questions max.

### 2. Check Local Resources First

Before hitting the web, check what you already have:
- `/agent/memory/` — have you done this before? Check journal and capabilities
- `/agent/workspace/` — any existing files, docs, or prior research?
- Existing scripts — many questions can be answered by running a local tool:

| Question Type | Script to Run |
|--------------|--------------|
| Agent growth / milestone narrative (cycles, goals, capabilities) | `scripts/milestone_report.py` |
| System maintenance / housekeeping status | `scripts/maintain.py` |
| Background service status on ports 8081–8090 | `scripts/service_manager.py list` |
| Log disk usage / clean old log files | `bash /agent/scripts/log_cleanup.sh --dry-run` (remove `--dry-run` to apply) |

Running a local script is faster, cheaper, and already logged — prefer it over web search when it covers the question.

### 3. Web Research (if local resources are insufficient)

Use **WebSearch**, **WebFetch**, or the **`agent-browser` skill**:

- **WebSearch** — best for: factual queries, news, quick lookups. Max 3 searches per question.
- **WebFetch** — best for: reading a specific URL (docs, blog posts). Works on static pages.
- **`agent-browser`** — use when: WebSearch is unavailable, the page requires JavaScript, you need to interact with a live site (forms, logins, scraping dynamic content), or you need to take a screenshot.

Search discipline:
- **Max 3 searches per question.** If you haven't found an answer in 3 searches,
  the question is probably too broad — narrow it down.
- **Prefer official docs** over blog posts or tutorials.
- **Note the date** of any information — stale docs cause bugs.
- **Log every web request** in your journal (constitution requires this).

### 4. Synthesize Findings

Before acting on research, write a brief summary:
- What did you learn? (2-3 bullet points)
- What's the recommended approach?
- What are the risks or unknowns?
- Is this enough to proceed, or do you need user input?

Write this summary to your journal. If the user needs to make a decision,
write it to `/agent/messages/outbox.json`.

### 5. Transition to Action

Once research is done:
- If you have enough info → proceed with the goal (switch to normal goal execution)
- If you need user input → write to outbox, set goal to "in_progress", wait
- If the goal is infeasible → mark as "failed" with explanation in journal

## Anti-Patterns

- **Rabbit holes:** Don't research tangential topics. Stay on the question.
- **Over-researching:** 1 cycle of research max. If you need more, you're
  probably trying to learn too much at once — break the goal into phases.
- **Ignoring local scripts:** Running a local script takes seconds; a
  manual web search for the same data takes minutes and yields less history.
- **Acting without understanding:** Don't install packages, write code, or
  create files until research is done. Wrong assumptions waste more cycles
  than research does.
- **Forgetting to log:** Every web fetch must be logged. This is a constitution
  requirement AND it helps future cycles avoid re-researching the same thing.

## Output

Your journal entry for a research cycle should follow this format:

```
**Research:** [question]
**Sources:** [URLs or local files consulted]
**Findings:** [2-3 bullet points]
**Decision:** [what you'll do next, or what user input you need]
```

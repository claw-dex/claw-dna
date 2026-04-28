# POST-GOAL REVIEW

Run this checklist after marking a goal as "completed" in goal.json.
Keep it brief — 2-3 minutes max. The value is in the habit, not the length.

## Quick Review Checklist

After completing a goal, ask yourself:

### 1. Deliverable Check

- Did I produce what was asked for? (Re-read the original goal content)
- Did I write output to the right place? (outbox for user, workspace for files)
- If the goal required portal/server changes, did I verify they work?

### 2. Efficiency Check

- How many cycles did this take? (Check your journal entries for this goal — count the entries with matching goal text)
- Could I have done it in fewer cycles? If so, what slowed me down?
- Did I have to restart the server manually? (Note it in journal entry if so)

### 3. Reusable Patterns

- Did I create anything reusable? (scripts, portal modules)?
- If yes: add the new script/module to the **Utility Scripts** section in `AGENTS.md` so future cycles can discover it
- Did I learn a new technique?
- If yes: Note it in the journal entry and create a skill in `skills/` directory if it is useful for future

### 4. User Communication

- Does the user know the goal is done? (Check outbox.json)
- If the deliverable is non-obvious (e.g., a file in workspace/), tell them where to find it
- If you lack capabilities to complete the goal, inform the user and ask for guidance. You may use `skills-sh-find-skills` to find a skill that will help you complete the goal and suggest it to the user.

## When to Skip

Skip this review if the goal was trivial (< 1 cycle, config change, simple fix).
Only run it for goals that involved real work — content creation, multi-step
implementation, research tasks, or anything that took > 1 cycle.

## 5. Learnings (Mandatory)

After completing any goal, you MUST document how you accomplished it by adding a
**"Learnings"** section to the journal entry for that cycle. This section should include:

1. **Approach taken** — what strategy/tools/skills you used to accomplish the goal
2. **Key decisions** — why you chose this approach over alternatives
3. **Reusable patterns** — any techniques, commands, or workflows that could apply to future goals
4. **Pitfalls encountered** — mistakes or dead ends you hit and how you resolved them

This ensures you build institutional knowledge over time. When starting a new goal,
always review recent journal entries for relevant learnings that could inform your approach.

## Output

Add a "Review" and "Learnings" section to your journal entry for the goal, e.g.:

```
**Review:** Delivered in 1 cycle. Output in workspace/myfile.md. Outbox response sent.
**Learnings:**
- Approach: Used ThreadPoolExecutor for parallel API calls
- Key decision: Batch API over individual lookups (10x fewer calls)
- Reusable: Persistent file cache pattern for expensive lookups
- Pitfall: Session-state cache lost on refresh — switched to file-based
```

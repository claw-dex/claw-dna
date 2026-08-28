---
name: skill-manage
description: Create, patch, edit, archive, or attribute use to skills in /agent/skills/ via the skill_manage CLI. Use this whenever you want to durably capture a reusable procedure as a skill, refine an existing skill with a learned improvement, consolidate two overlapping skills into an umbrella, archive an unused skill, or manually record that a skill helped on this cycle. Triggers: "save this as a skill", "remember how I did this", "patch the X skill", "merge X and Y skills", "delete the Z skill", "I just used skill X", "what skills do we have".
---

# skill-manage — durable skill lifecycle

`scripts/skill_manage.py` is the only sanctioned way to mutate the
`skills/` tree and its lifecycle metadata (`skills/.usage.json`). Hand-
editing SKILL.md works for content tweaks, but bypasses use/patch
counters and lifecycle state — prefer the CLI.

## When to invoke

- **You discovered a reusable procedure**: `create` a new skill.
- **An existing skill missed a case**: `patch` a new section onto it.
- **An existing skill is wrong end-to-end**: `edit` its body wholesale.
- **Two skills overlap**: `delete --absorbed-into <umbrella>` the redundant one.
- **A skill is dead weight, no umbrella fits**: `delete --prune`.
- **You followed a skill's instructions this cycle**: `use --name <X>` (optional; transcript scanning will also catch it).
- **You want to make a skill immune to auto-archive**: `touch --pin`.

## Subcommands

```
uv run python /agent/scripts/skill_manage.py init
uv run python /agent/scripts/skill_manage.py create  --name X --description "..." [--body-file PATH]
uv run python /agent/scripts/skill_manage.py patch   --name X --section "Section title" --body-file PATH
uv run python /agent/scripts/skill_manage.py edit    --name X --body-file PATH
uv run python /agent/scripts/skill_manage.py write-file --name X --rel-path scripts/foo.py --body-file PATH
uv run python /agent/scripts/skill_manage.py delete  --name X (--absorbed-into Y | --prune) [--force]
uv run python /agent/scripts/skill_manage.py use     --name X
uv run python /agent/scripts/skill_manage.py touch   --name X (--pin | --unpin)
uv run python /agent/scripts/skill_manage.py list    [--state active|stale|archived]
```

## Rules

- **Names**: lowercase letters, digits, hyphens; ≤ 64 chars.
- **Body files**: pass via `--body-file <path>` (write the body to a temp file first; the CLI does not accept inline newlines on argv).
- **delete** requires exactly one of `--absorbed-into <skill>` or `--prune`. The curator uses `absorbed_into` to classify the removal as a consolidation; `--prune` means "this skill has no successor."
- **Pinned skills cannot be deleted** without `--force`. Seed skills (every skill that existed before the learning loop shipped) are pinned by default.
- **`bump-usage` is for cycle_close** — do not invoke it yourself; it runs in a detached background process after every cycle.

## Lifecycle states

| State    | Meaning                                                                |
|----------|------------------------------------------------------------------------|
| active   | In rotation. Shown in the agent's skill catalog.                       |
| stale    | No activity ≥ 30 days. Still loadable, but flagged for review.         |
| archived | No activity ≥ 90 days. Directory moved to `skills/.archive/<name>-<date>/`. Restorable by moving back. |

Only `created_by:"agent"` + `pinned:false` skills auto-transition.

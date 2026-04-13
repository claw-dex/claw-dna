---
name: skills-clawhub-search-skills
description: Search and install agent skills from ClawHub, the public skill registry. Use when users ask to find, install, or update skills, or want to know what's available. Triggers on "find a skill for …", "search for skills", "install a skill", "what skills are available?", "update my skills". See full list of skills at https://clawhub.ai/skills.
---

# ClawHub

ClawHub is a public skill registry for AI agents. Search by natural language (vector search).

## Search

```bash
npx --yes clawhub@latest search "web scraping" --limit 5
```

## Install

```bash
npx --yes clawhub@latest install <slug> --workdir /agent
```

Replace `<slug>` with the skill name from search results. This places the skill into `/agent/skills/`. Always include `--workdir`.

## Update

```bash
npx --yes clawhub@latest update --all --workdir /agent
```

## List installed

```bash
npx --yes clawhub@latest list --workdir /agent
```

## Notes

- Requires Node.js (`npx` comes with it).
- No API key needed for search and install.
- `--workdir /agent` is critical — without it, skills install to the current directory instead of the agent workspace.
- After install, remind the user to start a new session to load the skill.
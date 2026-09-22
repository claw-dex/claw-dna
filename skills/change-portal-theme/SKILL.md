---
name: change-portal-theme
description: Change the visual theme of the Streamlit agent portal on user demand — colors, fonts, borders, dark/light base, and full re-skins. Use whenever the user asks to restyle the portal, switch between dark/light, apply a preset (Dracula, Nord, Spotify, GitHub, Stripe, Solarized, Snowflake, Minimal), tweak primary/background/text colors, change fonts (incl. Google Fonts or custom font files), adjust border radius / widget borders, or align the portal to a brand. Triggers: "change the portal theme", "make the portal dark/light", "use the X theme", "change colors", "change font", "make it look like Y", "rebrand the portal".
---

# Change the portal theme

Operational wrapper for restyling **this** portal. All theme syntax, options, design rules, and template catalog live in the upstream skill:

> `skills/developing-with-streamlit/skills/creating-streamlit-themes/SKILL.md`

Read it whenever you need option names, valid values, design principles, or the template list. This file only covers what's specific to this repo.

## Where to edit

**Only file to touch:** `/Users/vincent/workspace/claw-dex/claw-dna/.streamlit/config.toml`

Inside that file, modify **only** the `[theme]` block (and `[[theme.fontFaces]]` blocks if adding self-hosted fonts). **Never** touch `[server]` or `[browser]` — they control port, polling, upload limits, and static serving and must survive any theme change. Do not symlink or wholesale-replace the file.

## Decision flow

1. **Preset named** ("use Dracula", "make it Spotify-like") → read `skills/developing-with-streamlit/templates/themes/<name>/.streamlit/config.toml`, copy its `[theme]` block (and any `[[theme.fontFaces]]`) over the existing one.
2. **Dark / light flip** → set `base = "dark"` or `"light"`; remove conflicting overrides.
3. **Specific tweak** (brand color, font, radius) → edit individual keys under `[theme]`. Look up option names in the upstream skill.
4. **Ambiguous request** ("make it nicer") → use `AskUserQuestion` with 2–3 preset options and `preview` before editing.

## Applying the change

1. Edit the `[theme]` block in `claw-dna/.streamlit/config.toml`.
2. Validate TOML: `python3 -c "import tomllib; tomllib.load(open('claw-dna/.streamlit/config.toml','rb'))"`.
3. Most options take effect on browser refresh (portal runs with `runOnSave = true`).
4. **Restart required** if you added/changed `[[theme.fontFaces]]` — use the existing `server-restart` skill.
5. Tell the user to reload their browser tab.

## Repo-specific notes

- Bundled templates live at `skills/developing-with-streamlit/templates/themes/` (dracula, github, minimal, nord, snowflake, solarized-light, spotify, stripe). The upstream skill's templates table lists the primary color and bundled fonts for each.
- `enableStaticServing` is already configurable in this portal's `config.toml`, so self-hosted fonts via `[[theme.fontFaces]]` work — just remember the restart.
- **No custom CSS for theming.** Use `config.toml` only, unless the user explicitly asks for CSS. See the upstream skill's "No custom CSS" section.

## Checklist before declaring done

- [ ] Only `[theme]` / `[[theme.fontFaces]]` changed in `claw-dna/.streamlit/config.toml`.
- [ ] `[server]` and `[browser]` blocks untouched.
- [ ] TOML parses cleanly.
- [ ] Portal restarted if `[[theme.fontFaces]]` was modified.
- [ ] User told to reload the browser tab.

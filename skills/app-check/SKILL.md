---
name: app-check
description: Use after editing portal/Streamlit code to confirm the app still renders cleanly before restarting or handing back to the user. Triggers when the portal screen is blank, broken, or behaving unexpectedly after a code change, when the user reports "the portal won't load" or "something looks wrong after my edit", when verifying a fix worked end-to-end, or as a pre-restart safety check that catches errors the basic HTTP health probe would miss.
---

# app-check

**Path:** `scripts/app_check.py`

Performs a full headless render of `server.py` using Streamlit's `AppTest` framework. Detects errors the HTTP health endpoint cannot catch.

## Arguments

| Flag | Description |
|------|-------------|
| _(none)_ | Run the headless render check and print result |
| `--json` | Output result as JSON |

**Exit codes:** `0` = OK, `1` = FAIL, `2` = TIMEOUT, `3` = AppTest unavailable

## Examples

```bash
# Check if the portal renders cleanly
uv run python scripts/app_check.py

# JSON output for scripting
uv run python scripts/app_check.py --json
```

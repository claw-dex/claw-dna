---
name: app-check
description: Headless portal render check using Streamlit's AppTest framework. Catches syntax errors, missing imports, and runtime exceptions that the /_stcore/health endpoint misses. Use when the portal behaves unexpectedly after code changes, when you want to verify server.py loads without errors before restarting, or when debugging a blank/broken portal screen.
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

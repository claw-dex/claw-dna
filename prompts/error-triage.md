# ERROR TRIAGE: Systematic Debugging

Use this prompt when you encounter an error during goal execution — a failed
command, broken endpoint, unexpected behavior, or test failure. Don't guess
at fixes; diagnose systematically.

## Quick Fix: Server Restart (Most Common Issue)

If server changes aren't taking effect or you get connection refused:

```bash
bash /agent/scripts/server_restart.sh --verify
```

This triggers a Streamlit hot-reload (touches server.py) and verifies health via /_stcore/health.
If it doesn't resolve the issue, continue with full triage below.

## Triage Steps

### 1. Capture the Error

Before doing anything else, record:
- **What failed:** exact command, URL, or operation
- **Error message:** full text, not a summary
- **Context:** what were you doing when it failed? What cycle? What goal?

### 2. Run Diagnostics

Use the utility scripts before manual debugging (all in `/agent/scripts/`):

| Script | When to Use |
|--------|-------------|
| `bash /agent/scripts/health_check.sh --retries 3` | Portal or API not responding |
| `uv run python scripts/maintain.py --fix` | Corrupt JSON, missing files, stale data |
| `bash /agent/scripts/server_restart.sh --verify` | Server changes not taking effect, port conflicts |
| `uv run python scripts/self_test.py --record` | Full test suite (14 suites), logs failures to journal.json |

### 3. Classify the Error

| Category | Examples | Typical Fix |
|----------|----------|-------------|
| **Server** | Port in use, stale process, 500 error | `bash /agent/scripts/server_restart.sh --verify` |
| **File I/O** | Permission denied, corrupt JSON | `uv run python scripts/maintain.py --fix`, check paths |
| **Network** | Connection refused, timeout | `bash /agent/scripts/health_check.sh`, check ports |
| **Logic** | Wrong output, unexpected state | Read code, trace data flow |
| **Resource** | Out of memory, disk full | Clean up, check limits |
| **Import** | `ModuleNotFoundError`, tab crash on load | See Streamlit tab errors below |

### 3a. Streamlit-Specific Errors

**Tab crash (module import fails):**
```bash
# Check which tab is broken:
cat /agent/memory/server_errors.json
# Test the import directly:
uv run python -c "from app import <module_name>"
# After fixing the module, errors auto-clear in 48h or manually:
uv run python scripts/cycle_start.py --clear-old-errors
```

**`@st.cache_data` / TTLCache KeyError (banned pattern):**
- Never use `@st.cache_data` in `app/data.py`
- Most functions now use mtime-based caching (see `prompts/server.md` Data Layer section)
- For dynamic/computed data, use `@_cache(ttl=N)` from `app/data.py`
- Write ops must call `_cache_clear_all()`

**Streamlit hot-reload not picking up changes:**
```bash
bash /agent/scripts/server_restart.sh --verify
```

**New package not found after adding to pyproject.toml:**
```bash
uv sync  # must run after editing pyproject.toml
```

### 4. Check Known Issues

- Check auto memory `failures.md` — has this happened before?
- Check journal for similar symptoms in past cycles

### 5. Fix

- Apply the **minimal fix** that resolves the issue
- Don't refactor surrounding code while fixing a bug
- Test the fix immediately — don't assume it works

### 6. Record

After fixing, update auto memory `failures.md` with the failure pattern:
- Cycle number
- Symptom: what happened
- Diagnosis: root cause
- Fix: what you did
- Root cause: why it happened
- Prevention: how to avoid it in the future

## Common Pitfalls

- **Fixing symptoms, not causes:** If the server keeps crashing, find out why.
- **Cascading fixes:** If your fix breaks something else, stop and revert.
- **Not testing the fix:** Always verify with a concrete test before moving on.
- **Skipping scripts:** The utility scripts handle retries and edge cases that
  manual bash commands miss. Use them first.

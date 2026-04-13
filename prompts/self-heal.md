# SELF-HEAL: Web Portal is Unhealthy

> **Enum Reference:** See `prompts/enum.md` for valid `status` and `type` values used during self-heal cycles.

TOP PRIORITY. Do not work on any other goal until the portal is healthy.
The portal is your only communication channel with the user.

## Step 1: Quick Triage (use existing scripts)

```bash
# Fast automated check — tests server process, port, and Streamlit health endpoint
bash /agent/scripts/health_check.sh --retries 2 --delay 2

# Comprehensive Python health check
uv run python scripts/self_test.py --record

# Memory file integrity — scan and auto-fix
uv run python scripts/maintain.py --fix
```

If health-check passes, this may be a transient failure. Log it and move on.

## Step 2: Common Fixes (ordered by frequency)

### Fix A: Stale Process (MOST COMMON — recurring issue)

The process manager (PID 1) runs Caddy (8080) and Streamlit (8081).
The watchdog restarts crashed services every 10s. Stale processes may hold ports.

```bash
# Use the restart script — handles stale PID cleanup automatically
bash /agent/scripts/server_restart.sh --verify
```

If `server_restart.sh` fails, do it manually:
```bash
# Find and kill stale streamlit/server processes (never kill PID 1)
ps aux | grep -E "streamlit.*server|python.*server\.py" | grep -v grep | awk 'NR>1 && $2 != 1 {print $2}' | xargs -r kill
sleep 2
curl -s http://localhost:8081/app/_stcore/health
# NOTE: The watchdog in PID 1 (bootstrap.sh) will auto-restart Streamlit.
# Only restart manually if the watchdog itself is confirmed dead.
```

### Fix B: Server Syntax Error

```bash
uv run python -c "import py_compile; py_compile.compile('/agent/server.py')" 2>&1
```

If syntax error: fix server.py (or restore from backup). Watchfiles will
auto-reload, but if it doesn't, use Fix A to restart manually.

### Fix C: Corrupted or Missing State Files

The server's `_startup_check()` auto-fixes missing/corrupted JSON files on
every boot — it backs up corrupt files as `.corrupt` and recreates defaults.
**A server restart (Fix A) usually resolves this automatically.**

If you still need to check manually:
```bash
uv run python scripts/maintain.py --fix
```

If validation fails after a restart, reconstruct from journal.json.

### Fix D: Missing Python Module (`ModuleNotFoundError`)

If you see `ModuleNotFoundError: No module named 'xxx'`, the virtual environment is out of sync with `pyproject.toml`.

```bash
# Re-sync the environment — installs any missing dependencies
uv sync
```

If the module is not yet listed in `pyproject.toml`, add it first:
```bash
uv add <package-name>
# uv add automatically runs uv sync after updating pyproject.toml
```

Then verify the import works:
```bash
uv run python -c "import xxx; print('OK')"
```

**IMPORTANT:** Always run `uv sync` immediately after any change to `pyproject.toml`. This is the most common cause of `ModuleNotFoundError` — a dependency was added to `pyproject.toml` but `uv sync` was never run.

### Fix E: Streamlit Rendering Issue (server responds but page broken)

- Run self-test to identify specific failures: `uv run python scripts/self_test.py --record`
- Check for Python tracebacks via Streamlit logs or: `journalctl -u streamlit 2>/dev/null`
- Verify all app modules import cleanly (use dynamic list — hardcoded lists go stale):
  ```bash
  uv run python -c "
  import os, importlib
  mods = sorted([f[:-3] for f in os.listdir('/agent/app') if f.endswith('.py') and not f.startswith('__')])
  for m in mods:
      try: importlib.import_module(f'app.{m}'); print(f'  OK  app.{m}')
      except Exception as e: print(f'  FAIL app.{m}: {e}')
  "
  ```
- If a specific tab is broken, check the corresponding `app/*.py` module
- Restore from backup if you recently modified app files (look for `*.backup` files)

### Fix F: App Render Error (AppTest detected exception)

The heartbeat's periodic AppTest check found that `server.py` raises an exception
during headless rendering. The `/_stcore/health` endpoint still returned "ok"
because the Streamlit process is alive — but the Python app itself is broken.

Diagnostic commands:
```bash
# Reproduce the error
cd /agent && uv run python scripts/app_check.py

# Check the cached result
cat /agent/memory/app_check_result.json

# Verify syntax
uv run python -c "import py_compile; py_compile.compile('/agent/server.py')"

# Check all app module imports
uv run python -c "
import os, importlib
mods = sorted([f[:-3] for f in os.listdir('/agent/app') if f.endswith('.py') and not f.startswith('__')])
for m in mods:
    try: importlib.import_module(f'app.{m}'); print(f'  OK  app.{m}')
    except Exception as e: print(f'  FAIL app.{m}: {e}')
"
```

Common causes:
- **Missing dependency** → `uv sync` (or `uv add <package>` then `uv sync`)
- **Broken import chain** → a module in `app/` imports something that no longer exists
- **Error in `_startup_check()`** → the init routine in `app/shared.py` is failing
- **Incompatible Streamlit API** → a widget call uses a removed or renamed parameter

After fixing, verify: `uv run python scripts/app_check.py` should print `[app-check] OK`.

## Step 3: Emergency Fallback

If you cannot fix the root cause in this cycle, replace `server.py` with a
minimal Streamlit app that at minimum accepts commands and shows agent state:

```python
import streamlit as st, json, datetime
from pathlib import Path

INBOX  = Path("/agent/messages/inbox.json")
MEMORY = Path("/agent/memory/state.json")

st.title("Agent Portal [EMERGENCY MODE]")
st.warning("Portal is in emergency mode. The agent will restore it next cycle.")

try:
    state = json.loads(MEMORY.read_text())
    st.json(state)
except Exception as e:
    st.error(f"Cannot read state: {e}")

cmd = st.text_input("Send a command or goal")
if st.button("Send") and cmd:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    data = json.loads(INBOX.read_text()) if INBOX.exists() else []
    data.append({"type": "goal", "content": cmd, "timestamp": now})
    INBOX.write_text(json.dumps(data, indent=2))
    st.success("Queued in inbox.json")
```

Back up the original first: `cp /agent/server.py /agent/server.py.backup`

## Step 4: Verify

```bash
bash /agent/scripts/health_check.sh
```

All checks must pass before this cycle is complete.

## Step 5: Record the Failure

Follow the `/agent/prompts/cycle-close.md` checklist. Record the failure in your journal entry (via `cycle_close.py`) and update auto memory `failures.md` with:
- Symptom: what health check found
- Diagnosis: actual root cause
- Fix: what you did
- Root cause: why it broke
- Prevention: how to prevent

## Step 6: Update State

Set status to "recovering" during the fix, then "idle" once verified.

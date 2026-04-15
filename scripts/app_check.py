#!/usr/bin/env python3
"""Headless AppTest render check for server.py.

Uses Streamlit's built-in AppTest framework to do a full headless render,
catching syntax errors, missing imports, and runtime exceptions that the
/_stcore/health endpoint misses.

Exit codes:
  0 = OK (render completed without exceptions)
  1 = FAIL (render raised an exception)
  2 = TIMEOUT (render did not complete in time)
  3 = AppTest unavailable (Streamlit version too old or import error)
"""

import json
import os
import sys
import tempfile
from pathlib import Path

AGENT_DIR = Path("/agent")
RESULT_PATH = AGENT_DIR / "memory" / "app_check_result.json"


def write_result(status: str, detail: str = "", exceptions: list[str] | None = None):
    """Atomically write result JSON when --json flag is passed."""
    if "--json" not in sys.argv:
        return
    from datetime import datetime, timezone

    result = {
        "status": status,
        "detail": detail,
        "exceptions": exceptions or [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESULT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2))
    tmp.rename(RESULT_PATH)


def main() -> int:
    # Import AppTest
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError as e:
        msg = f"AppTest unavailable: {e}"
        print(f"[app-check] SKIP — {msg}")
        write_result("skip", msg)
        return 3

    # Change to /agent so relative imports in server.py work
    os.chdir(str(AGENT_DIR))

    # Run headless render
    try:
        at = AppTest.from_file("server.py", default_timeout=30)
        at.run()
    except TimeoutError as e:
        msg = f"Render timed out: {e}"
        print(f"[app-check] TIMEOUT — {msg}")
        write_result("timeout", msg)
        return 2
    except Exception as e:
        msg = f"AppTest.run() raised: {type(e).__name__}: {e}"
        print(f"[app-check] FAIL — {msg}")
        write_result("fail", msg, [str(e)])
        return 1

    # Check for exceptions captured during render
    if at.exception:
        exceptions = []
        # at.exception is a list of ExceptionElement objects
        for exc in at.exception:
            exc_str = str(exc.value) if hasattr(exc, "value") else str(exc)
            exceptions.append(exc_str)
        detail = "; ".join(exceptions[:3])
        print(f"[app-check] FAIL — {len(exceptions)} exception(s): {detail}")
        write_result("fail", detail, exceptions)
        return 1

    print("[app-check] OK")
    write_result("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
get_current_date_time.py — Show the current date/time in the user's configured timezone.

Reads the timezone from /agent/memory/portal_config.json (falls back to UTC).
Outputs human-readable text by default, or JSON with --json.

Usage:
    uv run python scripts/get_current_date_time.py
    uv run python scripts/get_current_date_time.py --json
"""

import argparse
import json
import pathlib
from datetime import datetime
import zoneinfo


def get_user_timezone() -> str:
    cfg = pathlib.Path("/agent/memory/portal_config.json")
    if cfg.exists():
        try:
            return json.loads(cfg.read_text()).get("timezone", "UTC")
        except (json.JSONDecodeError, OSError):
            pass
    return "UTC"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show current date/time in the user-configured timezone."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output as JSON instead of human-readable text.",
    )
    args = parser.parse_args()

    tz_name = get_user_timezone()
    tz = zoneinfo.ZoneInfo(tz_name)
    now = datetime.now(tz)

    if args.as_json:
        print(
            json.dumps(
                {
                    "timezone": tz_name,
                    "datetime": now.isoformat(),
                    "date": now.strftime("%Y-%m-%d"),
                    "time": now.strftime("%H:%M:%S"),
                    "weekday": now.strftime("%A"),
                    "utc_offset": now.strftime("%z"),
                },
                indent=2,
            )
        )
    else:
        print(f"Timezone : {tz_name}")
        print(f"Date     : {now.strftime('%Y-%m-%d')} ({now.strftime('%A')})")
        print(f"Time     : {now.strftime('%H:%M:%S')} (UTC{now.strftime('%z')})")


if __name__ == "__main__":
    main()

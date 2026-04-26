---
name: get-current-date-time
description: >
  Get the current date and time in the user's local timezone.
  Use whenever you need the correct local date/time — especially when the system clock (UTC)
  may not match the user's calendar day. Outputs text (default) or JSON (--json).
---

# get-current-date-time

Returns the current date and time in the user's local timezone.

- **Script**: `scripts/get_current_date_time.py`
- **Timezone source**: `/agent/memory/portal_config.json` → `timezone` field (falls back to `UTC`)

## When to use

- Any time you need today's date (e.g., dream phases, scheduling, journal entries, reports)
- When the system UTC date may not match the user's local calendar day
- When you need the timezone name alongside the date for display or storage

## Usage

```bash
# Human-readable text (default)
uv run python scripts/get_current_date_time.py

# JSON output (for scripting / parsing)
uv run python scripts/get_current_date_time.py --json
```

## Text output example

```
Timezone : Asia/Singapore
Date     : 2026-04-27 (Monday)
Time     : 00:25:51 (UTC+0800)
```

## JSON output example

```json
{
  "timezone": "Asia/Singapore",
  "datetime": "2026-04-27T00:25:51.859854+08:00",
  "date": "2026-04-27",
  "time": "00:25:51",
  "weekday": "Monday",
  "utc_offset": "+0800"
}
```

## JSON fields

| Field        | Description                                  |
|--------------|----------------------------------------------|
| `timezone`   | IANA timezone name (e.g. `Asia/Singapore`)   |
| `datetime`   | Full ISO 8601 datetime with UTC offset       |
| `date`       | `YYYY-MM-DD` — use this as `TODAY`           |
| `time`       | `HH:MM:SS` in local time                     |
| `weekday`    | Full weekday name (e.g. `Monday`)            |
| `utc_offset` | UTC offset string (e.g. `+0800`)             |

## Extracting just the date (bash)

```bash
TODAY=$(uv run python scripts/get_current_date_time.py --json | jq -r '.date')
echo "Today is: $TODAY"
```

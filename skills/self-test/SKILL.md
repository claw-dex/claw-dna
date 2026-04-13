---
name: self-test
description: Comprehensive agent self-test suite validating all critical systems: API endpoints, memory files, disk health, process state, app module imports, data loaders, and scripts. Use after major changes to confirm nothing is broken, during self-heal cycles to diagnose failures, or with --suite memory to run a specific subset. Use --record to persist failures to journal.json and --fail-fast to stop on the first failure.
---

# self-test

**Path:** `scripts/self_test.py`

Runs a comprehensive health check across all critical agent systems.

## Arguments

| Flag | Description |
|------|-------------|
| _(none)_ | Run all test suites and print results |
| `--json` | Output results as JSON |
| `--record` | Record failures to `journal.json` for future analysis |
| `--fail-fast` | Stop on the first failure |
| `--quiet` | Print only the summary line |
| `--list-suites` | List all available test suite names |
| `--suite NAME` | Run only the specified suite (e.g., `memory`, `scripts`, `api`) |
| `--cycle N` | Cycle number to attach when recording failures |

**Exit codes:** `0` = all passed, `1` = one or more failures

## Available Suites

Run `--list-suites` to see the current list. Common suites include: `memory`, `api`, `scripts`, `processes`, `disk`, `imports`, `loaders`.

## Examples

```bash
# Full test run
uv run python scripts/self_test.py

# Full run, record failures to journal
uv run python scripts/self_test.py --record --cycle 42

# Run only memory file tests
uv run python scripts/self_test.py --suite memory

# Stop on first failure (faster feedback)
uv run python scripts/self_test.py --fail-fast

# Quiet mode: summary only
uv run python scripts/self_test.py --quiet

# JSON output for scripting
uv run python scripts/self_test.py --json

# See all suite names
uv run python scripts/self_test.py --list-suites
```

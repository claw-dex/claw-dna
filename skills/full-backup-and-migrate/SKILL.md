---
name: full-backup-and-migrate
description: Full container backup and cross-container migration for the agent. On the source agent, creates a single timestamped zip containing all critical agent directories (memory, messages, app, scripts, skills, services, prompts, web, workspace, and selective /home/agent/ files) and instructs the user how to download it. On the target agent, restores from the uploaded zip to migrate all knowledge, memory, capabilities, and workspace files to the new container/runtime.
---

# full-backup-and-migrate

**Backup script:** `scripts/full_backup.sh`
**Restore script:** `scripts/full_restore.sh`

Use this skill to migrate an agent from one container/runtime to another. The procedure has three roles:

1. **Source agent** — runs the backup script, then tells the user where to download the zip.
2. **User** — downloads the backup zip from the source container and uploads it to the target container.
3. **Target agent** — invokes this same skill to perform full recovery, restoring memory, messages, workspace, code, and home-dir credentials/config.

The result is that all agent knowledge, memory, capabilities, prompts, skills, services, and workspace files are migrated to the new container.

---

## When to use which mode

| Situation | Mode |
|-----------|------|
| You are the **source** agent and need to hand off to a new container | Run **Backup** (below), then give the user the **Handoff instructions**. |
| You are the **target** agent and a backup zip has been uploaded to `/agent/backup/` | Run **Recovery**. |

If unsure which side you are on, check whether `/agent/backup/agent_full_backup_*.zip` already exists and was *just uploaded* by the user (target side) versus produced by you (source side). When in doubt, ask the user.

---

## Backup (source agent)

### What gets backed up

| Directory | Notes |
|-----------|-------|
| `/agent/memory/` | All JSON memory files; excludes `long_term_memory.lancedb/` (auto-rebuilt by `cycle_close.py` on the next cycle close, in a detached background process, from the restored JSON files) |
| `/agent/messages/` | inbox/outbox queues and history |
| `/agent/web/` | Static files |
| `/agent/workspace/` | Working files and cached data |
| `/agent/app/` | Streamlit portal modules |
| `/agent/prompts/` | Prompt templates |
| `/agent/scripts/` | Automation scripts |
| `/agent/skills/` | Skill definitions |
| `/agent/services/` | Background service files |
| `/agent/test/` | Test cases — packaged as `test.zip`. Restored with **prefer-current** semantics: existing test files on the target are kept; only new tests from the backup are seeded. |
| `/agent/` root files (selective) | `server.py`, `AGENTS.md`, `pyproject.toml`, `uv.lock`, `.streamlit/` — packaged as `root_files.zip`. Forbidden-to-modify root files (constitution.md, system.md, agent.sh, heartbeat.sh, bootstrap.sh, Caddyfile, app/commands_tab.py, scripts/app_check.py) are omitted; they come back from bootstrap. |
| `/home/agent/` (selective) | `.keepass/`, `.ssh/`, `.config/`, `.claude/` (excluding the `-agent` workspace), and root dotfiles |
| `git.zip` (optional) | Git history bundle of `/agent/.git` — used in Phase 4a to apply only the delta for files that already exist in the target repo. Absent if `git bundle` failed. |
| `git_head.txt` (optional) | Plain-text file containing `git rev-parse HEAD` from the source agent at backup time. Used in Phase 4b to determine ancestry (target ahead / backup ahead / diverged) when `git.zip` is absent, preventing git-tracked files from being silently regressed to an older backup version. |
| `caddy_config.json` (optional) | Live Caddy admin-API config snapshot from `localhost:2019/config/`. Used in Phase 4.5 to restore agent-driven Caddy routes (the static `Caddyfile` is read-only). Absent if the admin API was unreachable when the backup ran. |

### What gets cleaned before zipping

Before creating any zip, the script deletes:

- `*.backup` files in `/agent/memory/`
- `*.tmp` files in `/agent/memory/`
- `long_term_memory.lancedb.rebuild/` and `.backup/` leftover store directories

### What gets excluded from zips

- `*.lock` files (except `uv.lock`, which is captured intentionally inside `root_files.zip`)
- `__pycache__/` directories
- `.git/` directories — **except in `/agent/workspace/`, `/agent/web/`, and `/home/agent/`**, where nested `.git/` dirs of cloned repos are preserved. (Stripping them turns the clone into a plain dir tree; subsequent `git` commands then walk up to `/agent/.git` and silently attach to the wrong repo.)
- The migration tooling itself — `scripts/full_backup.sh`, `scripts/full_restore.sh`, and everything under `skills/full-backup-and-migrate/`. These are excluded from the backup so a future restore can never overwrite the live tooling with a stale copy. The target keeps whatever versions it already has; if you need to update them, do so via a normal commit, not via restore.

### Output

```
/agent/backup/agent_full_backup_<YYYYMMDDTHHMMSSZ>.zip
  ├── memory.zip
  ├── messages.zip
  ├── web.zip
  ├── workspace.zip
  ├── app.zip
  ├── prompts.zip
  ├── scripts.zip
  ├── skills.zip
  ├── services.zip
  ├── home_agent.zip
  ├── test.zip            (optional — restored with prefer-current semantics)
  ├── root_files.zip      (loose, optional)
  ├── git_head.txt        (loose, optional)
  └── caddy_config.json   (loose, optional)
```

### Usage

```bash
bash scripts/full_backup.sh
```

No arguments. The script prints progress and the final archive path and size.

### Handoff instructions (give these to the user verbatim)

After the backup completes, tell the user:

> Backup complete. Archive: `/agent/backup/agent_full_backup_<TIMESTAMP>.zip` (`<SIZE>`).
>
> **Next steps to migrate to the new agent container:**
>
> 1. **Download** the backup zip from this container to your local machine (portal file browser, or `scp <source-host>:/agent/backup/agent_full_backup_<TIMESTAMP>.zip ./`).
> 2. **Upload** the zip to the target agent container at `/agent/backup/` (create the directory if it does not exist):
>
>    ```bash
>    scp ./agent_full_backup_<TIMESTAMP>.zip <target-host>:/agent/backup/
>    ```
>
> 3. **On the target agent**, invoke this same skill (`full-backup-and-migrate`) and point it at the uploaded zip. The target agent will run the Recovery phases below.

Substitute the real timestamp, size, and host names. If the user's environment uses a different transport (cloud storage, browser download, etc.), adapt the wording but keep the three-step shape: download → upload → invoke skill on target.

---

## Recovery (target agent)

The deterministic phases of recovery are implemented in **`scripts/full_restore.sh`**. The agent runs the script; the script runs phases 0 → 5 in order. The agent only steps in for the few cases that genuinely need judgment (ambiguous backup zip, git merge conflicts, manual diff review when no `git.zip` is present, self-test failure).

### Step 1 — Locate the backup zip

The script does NOT auto-detect — you must pass the exact path. Find it under the standard upload locations first:

```bash
ls -1t /agent/backup/agent_full_backup_*.zip   2>/dev/null
ls -1t /agent/workspace/agent_full_backup_*.zip 2>/dev/null
```

Decide based on what you see:

- **Exactly one zip** across both directories → use it.
- **More than one zip** → **ask the user which one to restore** before continuing. Do not guess from filename timestamps; the user may have uploaded a fresh one alongside an older one and the right choice depends on intent.
- **None** → ask the user where they uploaded it, then locate it:

  ```bash
  find /agent /home/agent /tmp -maxdepth 5 -name "agent_full_backup_*.zip" 2>/dev/null
  ```

### Step 2 — Run the restore script

```bash
bash /agent/scripts/full_restore.sh /path/to/agent_full_backup_<TIMESTAMP>.zip
```

The path is required (no auto-detect — see Step 1).

The script runs these phases (mirrors the previous doc):

| Phase | What it does | Manual fallback? |
|-------|--------------|------------------|
| 0 | Stops services from `/agent/memory/services.json` | — |
| 1 | Unzips outer + inner zips into `/tmp/<restore_name>/` | — |
| 2 | Full-override copy of `memory/`, `messages/`, `workspace/`, `web/`, `home/agent/`, plus per-file restore of repo-root files (`server.py`, `AGENTS.md`, `pyproject.toml`, `uv.lock`, `.streamlit/`). Then deletes any `*.session` files under `/agent/memory/` and `/agent/messages/` — sessions are bound to the source environment (PIDs, tokens, sockets) and would attach to dead state if reused; removal forces a clean re-init in the target. | — |
| 2b | `cd /agent && uv sync` (always — the venv is not in the backup, so it must be reconciled to the restored `pyproject.toml`/`uv.lock` before services restart) | — |
| 3 | Selective copy of `app/`, `prompts/`, `scripts/`, `skills/`, `services/` (skips files that already exist on target); skipped paths logged to `/tmp/<restore_name>_skipped.txt` | — |
| 3b | **Prefer-current** copy of `test/`: existing test files are silently kept (so Phase 4a never patches them either); only new test files from the backup are seeded. Protects core test cases from being regressed by an older backup. | — |
| 4a | Git-bundle delta apply for *tracked, modified* files only — **per-file, no commits**. For each file in Phase 3's skipped list that (a) differs from target and (b) is tracked in `/agent`'s git repo, the script generates a single-file patch from the bundle and tries `git apply --3way`. Files that apply cleanly are left as **uncommitted working-tree changes**. Files that fail or leave conflict markers are reverted to HEAD (`git checkout -- <file>`) and listed in `/tmp/<restore_name>_needs_merge.txt`. Untracked-but-divergent files go to `/tmp/<restore_name>_untracked.txt`. The script never aborts on conflict — it skips and moves on. | When the run finishes, **resolve files in `_needs_merge.txt` manually** (use the bundle, the backup tree, or `git diff` against the listed paths). For files in `_untracked.txt`, use the **Phase 4b** ancestry check below. If `git.zip` is missing or no merge-base exists, the whole 4a path is skipped and **Phase 4b** runs automatically. |
| 4b | **Automated ancestry check** (runs only when Phase 4a was skipped). Reads `git_head.txt` from the backup and uses `git merge-base` to classify each divergent skipped file: `target_ahead` (backup is older → keep target's version), `backup_ahead` (backup is newer → apply backup's version), `diverged`/`unknown` (flag to `/tmp/<restore_name>_review_4b.txt` for manual resolution). Prevents git-tracked files from being silently regressed to an older backup version. | Resolve any files in `_review_4b.txt` manually — see Step 3 below. |
| 4.5 | POSTs `caddy_config.json` to `localhost:2019/load` to restore live Caddy routes (no-op if the file is absent) | If the POST fails: investigate Caddy with `ps`, container logs; re-run the snippet by hand. |
| 5 | Removes `/tmp/<restore_name>/`, moves backup zip into `/agent/backup/` if uploaded elsewhere, runs `uv run python scripts/self_test.py`, gateway probe, then `service_manager.py auto-start` + `health`. (The venv was already synced in Phase 2b.) | If `self_test.py` fails: read the output, fix the issue, then re-run the script with `--skip-self-test` once you've verified by hand. |

Script flags:

- `--skip-self-test` — skip the Phase 5 `self_test.py` + gateway probe.
- `--tail-only` — skip Phases 0–4a, run only Phase 4.5 + 5. Use after manually resolving Phase 4a conflicts so the already-restored `/agent/memory` and `/agent/workspace` are not overwritten a second time.
- `-h`, `--help` — usage.

**Resilience contract:** the script never aborts on a single-file failure. Bad inner zips, failed copies, conflict-on-apply, and unreachable Caddy admin all log a warning and continue. Only a self-test failure causes a non-zero exit, and even then services and Phase 4.5 still run. **On a self-test failure** the script leaves `/tmp/<restore_name>/` and the backup zip in place so you can re-run with `--tail-only` after fixing. On success it removes `/tmp/<restore_name>/` and moves the zip into `/agent/backup/`.

**Protected paths (never overwritten):** the restore script holds an internal `PROTECTED_PATHS` list (see `scripts/full_restore.sh` near the top). Files on this list are skipped by Phase 3 (selective copy) and Phase 4a (git-bundle patch) — you'll see `PROTECTED: <rel> — skipping (never overwrite)` in the output. You must not delete or override files in this list during the migration process.

### Step 3 — Handle conflicts or Phase 4b review items

The script prints either:

- `Phase 4a conflicts (skipped, reverted to HEAD): N file(s) — see /tmp/<restore_name>_needs_merge.txt` → review each path in that log, reconcile against the backup tree (still at `/tmp/<restore_name>/agent/<rel>` if Phase 5 self-test failed; otherwise re-extract the backup zip), edit in `/agent/<rel>`, and leave the result as uncommitted working-tree changes. No need to re-run the script for this — Phases 0–4 already happened and Phase 4.5/5 succeeded. Only re-run `--tail-only` if the self-test was failing because of these unresolved files.
- `Needs manual review: N file(s) — see /tmp/<restore_name>_review_4b.txt` → Phase 4b ran but found files it couldn't classify automatically (`diverged` or `unknown` ancestry). Review each path in that log.

#### Phase 4b — what the script does automatically

When `git.zip` is missing or Phase 4a was skipped, Phase 4b reads `git_head.txt` from the backup and calls `git merge-base` to determine ancestry:

| Ancestry result | Meaning | Action taken |
|-----------------|---------|--------------|
| `target_ahead` | Backup HEAD is an ancestor of target HEAD — backup is older | Git-tracked divergent files: **keep target's version** (logged as `KEPT_TARGET` in `_review_4b.txt`) |
| `backup_ahead` | Target HEAD is an ancestor of backup HEAD — backup is newer | Divergent files: **apply backup's version** (logged as `APPLIED_BACKUP`) |
| `diverged` | Commits have forked since the common ancestor | Flag to `_review_4b.txt` as `REVIEW_NEEDED` for manual resolution |
| `unknown` | `git_head.txt` absent, backup HEAD not in target repo, or git errors | Same as `diverged` — flag for manual review |

**Critical safety guarantee:** a file is never silently overwritten with an older backup version. `target_ahead` always keeps the target's (newer) copy.

#### Manual review for `_review_4b.txt` items

For files listed as `REVIEW_NEEDED` in `/tmp/<restore_name>_review_4b.txt`, inspect each manually:

```bash
RESTORE_DIR=/tmp/<restore_name>           # wherever the script left it
REVIEW_LOG=/tmp/<restore_name>_review_4b.txt

grep REVIEW_NEEDED "$REVIEW_LOG" | awk '{print $NF}' | while IFS= read -r rel; do
  src_file="$RESTORE_DIR/agent/$rel"
  dst_file="/agent/$rel"
  [ -f "$src_file" ] || continue
  echo ""
  echo "=== DIFF: $rel ==="
  diff "$src_file" "$dst_file" || true
done
```

Decide per file:

- **Backup has additions target lacks** → manually apply (e.g. new functions, new config keys)
- **Target is ahead** → keep target; backup is stale for this file
- **Both changed differently** → merge manually, keeping functional correctness of the current file

> **Never delete a file on the target just because it is absent from the backup.** The backup is a point-in-time snapshot — a file missing from it was either added after the backup was taken or was never included in that particular backup run. Absence in the backup is not evidence of deletion. Only remove a file from the target if you have explicit evidence (e.g. a `git rm` commit) that it was intentionally deleted.

### Step 4 — Self-test failure troubleshooting

If the script exits with `Restore finished WITH WARNINGS (self_test rc=…)`:

1. Re-run `cd /agent && uv run python scripts/self_test.py` and read the failure.
2. Common causes: missing dep (run `uv sync`), missing env var, a service still down (`service_manager.py health`), a config file in `/agent/memory/` that didn't restore cleanly.
3. After fixing, re-run `bash /agent/scripts/full_restore.sh --tail-only [PATH_TO_ZIP]` to re-verify and bring services up. Add `--skip-self-test` if you've already verified by hand.

### Step 5 — Run the test suite and fix regressions

The migration is not complete until `/agent/test/` passes on the target. The test tree was restored with **prefer-current** semantics in Phase 3b, so the target's existing tests are intact and any new tests from the backup were seeded in.

Run the full suite:

```bash
cd /agent && uv run pytest test/ -x --tb=short
```

(Drop `-x` to see all failures at once; keep `-x` for a faster fix-loop.)

**If everything passes:** migration is done. Commit any uncommitted Phase 4a working-tree changes that you've reviewed and want to keep.

**Failure-rate gate — stop and report if too many failed:**

Before attempting any fixes, check the failure ratio. If **more than 30%** of the collected test cases failed, **do not attempt to fix anything**. A failure rate that high almost always means the restore introduced a systemic problem (wrong dependency version, missing service, wrong env, wrong code branch applied) — fixing tests one at a time will mask the real issue and consume time. Instead:

1. Capture the totals (`pytest` reports them at the bottom: `N passed, M failed, K errors`).
2. Save the failure summary somewhere durable (e.g. `pytest test/ --tb=short > /tmp/restore_test_failures.txt 2>&1`).
3. Report to the user: total failed, total passed, percentage failed, and the top few distinct error messages. **Stop the migration here** and wait for guidance — do not start editing code or tests.

If the failure rate is **≤30%**, proceed with the per-failure fix loop below.

**If tests fail (≤30%):** treat each failure as a regression introduced by the restore — typically because Phase 4a applied a delta that doesn't quite fit the target's current code, or Phase 3b seeded a test that exercises a code path the target hasn't picked up yet.

For each failure:

1. **Default assumption: the test is correct, the agent codebase is wrong.** Fix the production code under `/agent/app/`, `/agent/scripts/`, `/agent/services/`, etc. — not the test. The whole point of preserving tests via prefer-current is that they encode behaviour the target should still satisfy.
2. Read the failure, locate the production code path it exercises, and reconcile it with what the test expects. The Phase 4a working-tree changes (uncommitted) and `_needs_merge.txt` / `_review_4b.txt` lists are the most likely culprits — start there.
3. Only modify the test itself if you have a clear reason: e.g., the test was written against an older API that the agent has since intentionally changed, or it depends on infrastructure that genuinely doesn't exist on the target. State the reason in the commit message.
4. Re-run `uv run pytest test/<failing_file>::<failing_case>` until green, then re-run the full suite.

When the suite is green and the agent is healthy (`service_manager.py health` clean, `self_test.py` passing), the migration is complete.

### Semantic memory index — auto-rebuilt by `cycle_close.py`

The backup intentionally omits `long_term_memory.lancedb/`. Do **not** run `memory_ingest.py --build` by hand — `cycle_close.py` detects the missing store on the next cycle close and rebuilds it from the restored JSON files (`journal.json`, `journal_archive.json`, `messages/inbox_history.json`) in a detached background subprocess (see `scripts/cycle_close.py:_flush_ltm_buffer` and `_dispatch_ltm_flush_bg`). The cycle that triggers the rebuild does not need to wait for it; durability follows ~5–10s later, and progress is logged to `/agent/memory/.ltm_flush.log`.

The rebuild embeds every record locally with fastembed, which needs the `BAAI/bge-small-en-v1.5` weights in the fastembed cache. On a fresh container run `bash seed/install_memory_deps.sh` first so the ~130 MB download happens once, outside a cycle.

```bash
uv run python scripts/memory_ingest.py --build
```

(Only run this manually if you need the index *immediately* and can't wait for the next cycle close.)

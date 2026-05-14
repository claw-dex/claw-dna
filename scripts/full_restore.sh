#!/usr/bin/env bash
# Full agent restore — companion to scripts/full_backup.sh.
# Runs the deterministic phases of the full-backup-and-migrate skill end-to-end.
# Phases that need human/agent judgment (locating an ambiguous zip, resolving
# git conflicts, manual diff review) stay in skills/full-backup-and-migrate/SKILL.md.
#
# Usage:
#   bash scripts/full_restore.sh BACKUP_ZIP
#
# Options:
#   --skip-self-test     Skip Phase 5 self_test.py (for re-runs after manual fix)
#   --tail-only          Run only Phase 4.5 + 5 (skip stop/unzip/copy/git-apply).
#                        Use after resolving Phase 4a conflicts manually so the
#                        already-restored /agent/memory and /agent/workspace are
#                        NOT clobbered a second time.
#   -h, --help           Show this help
#
# Phase 4a applies the git-bundle delta per-file. Files that apply cleanly
# are left as uncommitted working-tree changes (no commit is created). Files
# that conflict are reverted to HEAD and listed in
# /tmp/<restore_name>_needs_merge.txt for manual resolution by the agent.
#
# BACKUP_ZIP is required — pass the exact path. Auto-detection lives in the
# skills/full-backup-and-migrate/SKILL.md "Locate the backup zip" step because
# it may need user confirmation when multiple zips are present.
set -euo pipefail

# ── Args ──────────────────────────────────────────────────────────────────────
BACKUP_ZIP=""
SKIP_SELF_TEST=0
TAIL_ONLY=0

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --skip-self-test) SKIP_SELF_TEST=1 ;;
    --tail-only) TAIL_ONLY=1 ;;
    --) shift; BACKUP_ZIP=${1:-}; break ;;
    -*) echo "ERROR: unknown flag: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [ -n "$BACKUP_ZIP" ]; then
        echo "ERROR: multiple positional args (already have '$BACKUP_ZIP', got '$1')" >&2
        exit 2
      fi
      BACKUP_ZIP=$1
      ;;
  esac
  shift
done

# ── Validate backup zip ───────────────────────────────────────────────────────
if [ -z "$BACKUP_ZIP" ]; then
  echo "ERROR: BACKUP_ZIP path is required." >&2
  echo "       See skills/full-backup-and-migrate/SKILL.md 'Locate the backup zip' for how to pick the right one." >&2
  usage >&2
  exit 2
fi

if [ ! -f "$BACKUP_ZIP" ]; then
  echo "ERROR: backup zip not found: $BACKUP_ZIP" >&2
  exit 2
fi

RESTORE_NAME=$(basename "$BACKUP_ZIP" .zip)
RESTORE_DIR=/tmp/${RESTORE_NAME}
SKIPPED_LOG=/tmp/${RESTORE_NAME}_skipped.txt
BACKUP_REPO=/tmp/${RESTORE_NAME}_git

# Files this script must NEVER overwrite — replacing them mid-restore with
# a stale or incompatible backup version would break the in-flight restore,
# the next bootstrap, or the agent's safety contract. Paths are repo-relative
# (no leading /agent/). Excluded from Phase 3 (selective copy) and Phase 4a
# (git-bundle patch).
#
# Sources:
#   * Backup/restore tooling itself.
#   * Memory CLI scripts the running agent relies on (a stale copy can break
#     /agent/memory access mid-restore or in the very next cycle).
#   * Every "Hard Rule" file from constitution.md (NEVER delete/modify list).
PROTECTED_PATHS=(
  # Migration tooling
  "scripts/full_backup.sh"
  "scripts/full_restore.sh"
  "skills/full-backup-and-migrate/SKILL.md"
  # Memory CLI scripts the agent depends on at runtime
  "scripts/memory_ingest.py"
  "scripts/memory_ask.py"
  "scripts/memory_recall.py"
  # Constitution Hard-Rule "NEVER delete/modify" files
  "constitution.md"
  "system.md"
  "agent.sh"
  "heartbeat.sh"
  "bootstrap.sh"
  "Caddyfile"
  "app/commands_tab.py"
  "scripts/app_check.py"
  # (Directory prefixes use a trailing slash — the matcher below treats them
  # as "everything under this dir". `test/` is intentionally NOT here: it has
  # its own prefer-current restore in Phase 3 so new test files can seed onto
  # a fresh container while existing ones are kept.)
)
# Match an entry exactly (no trailing slash) or as a directory prefix
# (trailing slash). Path arg is repo-relative.
is_protected() {
  local rel=$1
  local p
  for p in "${PROTECTED_PATHS[@]}"; do
    if [[ "$p" == */ ]]; then
      case "$rel/" in
        "$p"*) return 0 ;;
      esac
    else
      [ "$rel" = "$p" ] && return 0
    fi
  done
  return 1
}

# Always clean up the temp git clone + the _backup remote so a re-run after a
# crash mid-Phase-4a doesn't trip on stale state. RESTORE_DIR is intentionally
# NOT cleaned here — it's needed for --tail-only re-runs and Phase 4b.
cleanup_git() {
  git -C /agent remote remove _backup 2>/dev/null || true
  rm -rf "$BACKUP_REPO"
}
trap cleanup_git EXIT

if [ $TAIL_ONLY -eq 0 ]; then
  : # Phase 2b runs `uv sync` unconditionally — no pre-snapshot needed.
else
  echo "--tail-only set: skipping Phases 0-4a (assumes restore already ran once)."
  if [ ! -d "$RESTORE_DIR" ]; then
    echo "WARNING: $RESTORE_DIR is missing — re-unzipping $BACKUP_ZIP for Phase 4.5 ..."
    mkdir -p "$RESTORE_DIR"
    unzip -o "$BACKUP_ZIP" -d "$RESTORE_DIR" >/dev/null
    for z in "$RESTORE_DIR"/*.zip; do
      [ -f "$z" ] || continue
      unzip -o "$z" -d "$RESTORE_DIR" >/dev/null
    done
  fi
fi

# ── Phase 0: Stop services ────────────────────────────────────────────────────
if [ $TAIL_ONLY -eq 0 ]; then
  echo ""
  echo "[0/6] Stopping services from /agent/memory/services.json ..."
  if [ -f /agent/memory/services.json ]; then
    for name in $(python3 -c "import json; print(' '.join(json.load(open('/agent/memory/services.json'))))" 2>/dev/null); do
      echo "  Stopping $name ..."
      python3 /agent/scripts/service_manager.py stop "$name" || true
    done
    python3 /agent/scripts/service_manager.py list || true
  else
    echo "  /agent/memory/services.json not found — fresh container, nothing to stop."
  fi

  # ── Phase 1: Unzip ──────────────────────────────────────────────────────────
  echo ""
  echo "[1/6] Unzipping $BACKUP_ZIP → $RESTORE_DIR ..."
  mkdir -p "$RESTORE_DIR"
  if ! unzip -o "$BACKUP_ZIP" -d "$RESTORE_DIR" >/dev/null; then
    echo "  WARNING: outer unzip reported errors — continuing with whatever extracted."
  fi
  for z in "$RESTORE_DIR"/*.zip; do
    [ -f "$z" ] || continue
    echo "  Expanding $(basename "$z")"
    if ! unzip -o "$z" -d "$RESTORE_DIR" >/dev/null; then
      echo "  WARNING: failed to expand $(basename "$z") — skipping."
    fi
  done
fi

if [ $TAIL_ONLY -eq 0 ]; then
  # ── Phase 2: Full-override copy ─────────────────────────────────────────────
  # Per-source resilience: a missing or broken inner zip's tree just warns
  # and the script continues with the rest of the directories.
  echo ""
  echo "[2/6] Full-override copy of memory/messages/workspace/web/home_agent ..."
  copy_tree() {
    local src=$1
    local dst=$2
    if [ ! -d "$src" ]; then
      echo "  WARNING: $src missing in backup — skipping."
      return 0
    fi
    if ! cp -rf "$src" "$dst"; then
      echo "  WARNING: cp $src → $dst reported errors — continuing."
    fi
  }
  copy_tree "$RESTORE_DIR/agent/memory/."    /agent/memory/
  copy_tree "$RESTORE_DIR/agent/messages/."  /agent/messages/
  copy_tree "$RESTORE_DIR/agent/workspace/." /agent/workspace/
  copy_tree "$RESTORE_DIR/agent/web/."       /agent/web/
  copy_tree "$RESTORE_DIR/home/agent/."      /home/agent/

  # Repo-root agent-modifiable files (per-file guards — older backups omit some).
  if [ -f "$RESTORE_DIR/root_files.zip" ] || [ -e "$RESTORE_DIR/agent/server.py" ] \
     || [ -d "$RESTORE_DIR/agent/.streamlit" ]; then
    for p in server.py AGENTS.md pyproject.toml uv.lock; do
      if [ -f "$RESTORE_DIR/agent/$p" ]; then
        cp -f "$RESTORE_DIR/agent/$p" "/agent/$p" \
          || echo "  WARNING: failed to restore /agent/$p — continuing."
      fi
    done
    if [ -d "$RESTORE_DIR/agent/.streamlit" ]; then
      cp -rf "$RESTORE_DIR/agent/.streamlit/." /agent/.streamlit/ \
        || echo "  WARNING: failed to restore /agent/.streamlit — continuing."
    fi
    echo "  Restored repo-root agent-modifiable files."
  else
    echo "  No root_files.zip in this backup — skipping repo-root file restore."
  fi

  # ── Phase 2b: uv sync (always runs) ─────────────────────────────────────────
  # Always sync — the venv must match the restored pyproject.toml/uv.lock
  # before services restart, regardless of whether the manifest "looks"
  # changed. Cheap when in-sync, mandatory when not.
  echo ""
  echo "[2b/6] Running uv sync ..."
  if ! (cd /agent && uv sync); then
    echo "  WARNING: uv sync failed — re-run manually before relying on the venv."
  fi

  # ── Phase 3: Selective copy (no override) ───────────────────────────────────
  echo ""
  echo "[3/6] Selective copy of app/prompts/scripts/skills/services ..."
  : > "$SKIPPED_LOG"

  # Loop runs in the parent shell (process substitution, not pipe).
  # -print0/read -d '' tolerates filenames with newlines. Per-file failures
  # log a warning and move on instead of aborting the entire phase.
  selective_copy() {
    local src_root=$1
    local dst_root=$2
    local src_file rel dst_file repo_rel

    if [ ! -d "$src_root" ]; then
      echo "  WARNING: $src_root missing in backup — skipping."
      return 0
    fi

    while IFS= read -r -d '' src_file; do
      rel="${src_file#$src_root/}"
      dst_file="$dst_root/$rel"
      # Repo-relative path (e.g. scripts/full_restore.sh) for protection check.
      repo_rel="${dst_file#/agent/}"

      if is_protected "$repo_rel"; then
        echo "  PROTECTED: $repo_rel — skipping (never overwrite)"
        continue
      fi

      if [ -e "$dst_file" ]; then
        printf '%s\n' "$dst_file" >> "$SKIPPED_LOG"
      else
        mkdir -p "$(dirname "$dst_file")" \
          || { echo "  WARNING: mkdir for $dst_file failed — skipping."; continue; }
        cp "$src_file" "$dst_file" \
          || echo "  WARNING: cp $src_file → $dst_file failed — skipping."
      fi
    done < <(find "$src_root" -type f -print0)
  }

  selective_copy "$RESTORE_DIR/agent/app"      /agent/app
  selective_copy "$RESTORE_DIR/agent/prompts"  /agent/prompts
  selective_copy "$RESTORE_DIR/agent/scripts"  /agent/scripts
  selective_copy "$RESTORE_DIR/agent/skills"   /agent/skills
  selective_copy "$RESTORE_DIR/agent/services" /agent/services

  SKIPPED_COUNT=$(wc -l < "$SKIPPED_LOG" | tr -d ' ')
  echo "  Skipped (already exist): $SKIPPED_COUNT files — log: $SKIPPED_LOG"

  # ── Phase 3b: prefer-current copy for /agent/test ───────────────────────────
  # Test files use "always prefer the current target version" semantics:
  # existing files are silently kept (NOT added to SKIPPED_LOG, so Phase 4a
  # never patches them), and only NEW files from the backup are seeded.
  # This protects core test cases on the target from being regressed by an
  # older backup while still allowing a fresh container to receive tests.
  echo ""
  echo "[3b/6] Prefer-current copy for /agent/test (only new files seeded) ..."
  prefer_current_copy() {
    local src_root=$1
    local dst_root=$2
    local src_file rel dst_file kept=0 added=0

    if [ ! -d "$src_root" ]; then
      echo "  WARNING: $src_root missing in backup — skipping."
      return 0
    fi

    while IFS= read -r -d '' src_file; do
      rel="${src_file#$src_root/}"
      dst_file="$dst_root/$rel"

      if [ -e "$dst_file" ]; then
        kept=$((kept + 1))
      else
        mkdir -p "$(dirname "$dst_file")" \
          || { echo "  WARNING: mkdir for $dst_file failed — skipping."; continue; }
        if cp "$src_file" "$dst_file"; then
          added=$((added + 1))
        else
          echo "  WARNING: cp $src_file → $dst_file failed — skipping."
        fi
      fi
    done < <(find "$src_root" -type f -print0)

    echo "  Kept current: $kept, Added new: $added"
  }
  prefer_current_copy "$RESTORE_DIR/agent/test" /agent/test

  # ── Phase 4a: git-bundle delta apply (per-file, no commits) ─────────────────
  echo ""
  echo "[4/6] Phase 4a — git-bundle delta apply for skipped files ..."
  NEEDS_MANUAL_4B=0
  BUNDLE_ZIP="$RESTORE_DIR/git.zip"
  MANUAL_REVIEW_LOG=/tmp/${RESTORE_NAME}_needs_merge.txt
  UNTRACKED_LOG=/tmp/${RESTORE_NAME}_untracked.txt
  : > "$MANUAL_REVIEW_LOG"
  : > "$UNTRACKED_LOG"

  if [ -f "$BUNDLE_ZIP" ]; then
    # Each setup step is guarded so a corrupted bundle / broken .git /
    # offline state never aborts the script. On any setup failure we mark
    # the whole 4a path as failed and fall through to manual review.
    SETUP_OK=1
    if ! unzip -jo "$BUNDLE_ZIP" git_history.bundle -d /tmp/ >/dev/null 2>&1; then
      echo "  WARNING: failed to extract git_history.bundle from $BUNDLE_ZIP."
      SETUP_OK=0
    fi
    if [ $SETUP_OK -eq 1 ]; then
      rm -rf "$BACKUP_REPO"
      if ! git clone --quiet /tmp/git_history.bundle "$BACKUP_REPO" 2>/dev/null; then
        echo "  WARNING: failed to clone backup git bundle."
        SETUP_OK=0
      fi
      rm -f /tmp/git_history.bundle
    fi
    if [ $SETUP_OK -eq 1 ]; then
      git -C /agent remote remove _backup 2>/dev/null || true
      if ! git -C /agent remote add _backup "$BACKUP_REPO" 2>/dev/null; then
        echo "  WARNING: failed to add _backup remote."
        SETUP_OK=0
      fi
    fi
    if [ $SETUP_OK -eq 1 ] && ! git -C /agent fetch _backup --quiet 2>/dev/null; then
      echo "  WARNING: failed to fetch from _backup remote."
      SETUP_OK=0
    fi

    BACKUP_HEAD=""
    MERGE_BASE=""
    if [ $SETUP_OK -eq 1 ]; then
      BACKUP_HEAD=$(git -C "$BACKUP_REPO" rev-parse HEAD 2>/dev/null || echo "")
      MERGE_BASE=$(git -C /agent merge-base HEAD "$BACKUP_HEAD" 2>/dev/null || echo "")
    fi

    if [ $SETUP_OK -eq 1 ] && [ -n "$MERGE_BASE" ]; then
      echo "  Merge base: $MERGE_BASE"
      echo "  Backup HEAD: $BACKUP_HEAD"

      # Filter SKIPPED_LOG to tracked + divergent files. Untracked files are
      # logged for manual review (no git history to 3-way merge against).
      # Protected paths are never patched.
      PATHSPECS=()
      while IFS= read -r dst_file; do
        rel="${dst_file#/agent/}"
        src_file="$RESTORE_DIR/agent/$rel"
        [ -f "$src_file" ] || continue
        if is_protected "$rel"; then
          echo "  PROTECTED: $rel — skipping (never overwrite)"
          continue
        fi
        diff -q "$src_file" "$dst_file" > /dev/null 2>&1 && continue
        if ! git -C /agent ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
          printf '%s\n' "$dst_file" >> "$UNTRACKED_LOG"
          continue
        fi
        PATHSPECS+=("$rel")
      done < "$SKIPPED_LOG"

      UNTRACKED_COUNT=$(wc -l < "$UNTRACKED_LOG" | tr -d ' ')
      if [ "$UNTRACKED_COUNT" -gt 0 ]; then
        echo "  $UNTRACKED_COUNT untracked-but-divergent file(s) — see $UNTRACKED_LOG"
        echo "  (left untouched; review manually via Phase 4b diff loop)"
      fi

      APPLIED=0
      CONFLICTED=0
      for rel in "${PATHSPECS[@]}"; do
        # Every per-file step is guarded so one broken path never aborts
        # the loop. Worst case: file is added to MANUAL_REVIEW_LOG and we
        # move on.
        PATCH=$(mktemp -t restore_patch_XXXXXX 2>/dev/null) || PATCH=""
        if [ -z "$PATCH" ]; then
          echo "  WARNING: mktemp failed for $rel — skipping."
          printf '%s\n' "$rel" >> "$MANUAL_REVIEW_LOG"
          CONFLICTED=$((CONFLICTED + 1))
          continue
        fi

        if ! git -C /agent diff "$MERGE_BASE" "$BACKUP_HEAD" -- "$rel" > "$PATCH" 2>/dev/null; then
          rm -f "$PATCH"
          printf '%s\n' "$rel" >> "$MANUAL_REVIEW_LOG"
          CONFLICTED=$((CONFLICTED + 1))
          continue
        fi
        if [ ! -s "$PATCH" ]; then
          rm -f "$PATCH"
          continue
        fi

        APPLY_RC=0
        git -C /agent apply --3way --ignore-whitespace "$PATCH" >/dev/null 2>&1 \
          || APPLY_RC=$?

        # A clean apply leaves no conflict markers; --3way can leave markers
        # even when apply itself returned 0. Check both. `git diff --check`
        # exits non-zero when markers are found.
        HAS_MARKERS=0
        git -C /agent diff --check -- "$rel" >/dev/null 2>&1 || HAS_MARKERS=1

        if [ $APPLY_RC -ne 0 ] || [ $HAS_MARKERS -eq 1 ]; then
          # Revert this file's working-tree changes (and any partial conflict
          # markers) so the target is back to HEAD for this path. Working tree
          # was clean entering Phase 4a (Phase 3 only adds new files), so
          # checkout is safe — it just undoes the failed apply. No commits.
          git -C /agent checkout -- "$rel" 2>/dev/null || true
          printf '%s\n' "$rel" >> "$MANUAL_REVIEW_LOG"
          CONFLICTED=$((CONFLICTED + 1))
        else
          APPLIED=$((APPLIED + 1))
        fi
        rm -f "$PATCH"
      done

      echo "  Applied cleanly: $APPLIED file(s) (uncommitted working-tree changes)"
      if [ $CONFLICTED -gt 0 ]; then
        echo "  Conflicts (skipped, reverted to HEAD): $CONFLICTED file(s) — see $MANUAL_REVIEW_LOG"
        echo "  Resolve these manually before committing or running cycle work."
      fi
    else
      if [ $SETUP_OK -eq 0 ]; then
        echo "  Bundle setup failed — fall back to manual diff review (Phase 4b)."
      else
        echo "  No common ancestor found — fall back to manual diff review (Phase 4b)."
      fi
      NEEDS_MANUAL_4B=1
    fi
    cleanup_git
  else
    echo "  No git.zip in backup — fall back to manual diff review (Phase 4b)."
    NEEDS_MANUAL_4B=1
  fi

  # ── Phase 4b: Automated ancestry check ──────────────────────────────────────
  # Runs only when Phase 4a was skipped (no git.zip or no merge-base). Reads
  # git_head.txt from the backup to determine whether the target is ahead,
  # behind, or diverged from the backup, then applies that logic per-file so
  # git-tracked files are never silently regressed to an older backup version.
  if [ $NEEDS_MANUAL_4B -eq 1 ] && [ "$SKIPPED_COUNT" -gt 0 ]; then
    echo ""
    echo "[4b/6] Phase 4b — git-ancestry safety check for skipped+divergent files ..."
    REVIEW_4B_LOG=/tmp/${RESTORE_NAME}_review_4b.txt
    : > "$REVIEW_4B_LOG"
    AUTO_KEPT_4B=0
    AUTO_APPLIED_4B=0
    NEEDS_HUMAN_4B=0
    ANCESTRY="unknown"

    GIT_HEAD_FILE="$RESTORE_DIR/git_head.txt"
    if [ -f "$GIT_HEAD_FILE" ]; then
      BACKUP_GIT_HEAD=$(tr -d '[:space:]' < "$GIT_HEAD_FILE")
      TARGET_HEAD=$(git -C /agent rev-parse HEAD 2>/dev/null || echo "")
      echo "  Backup git HEAD : ${BACKUP_GIT_HEAD:-n/a}"
      echo "  Target git HEAD : ${TARGET_HEAD:-n/a}"

      if [ -n "$BACKUP_GIT_HEAD" ] && [ -n "$TARGET_HEAD" ]; then
        # Verify backup HEAD is known to the target repo before calling merge-base
        if git -C /agent cat-file -e "${BACKUP_GIT_HEAD}^{commit}" 2>/dev/null; then
          MERGE_BASE_4B=$(git -C /agent merge-base "$TARGET_HEAD" "$BACKUP_GIT_HEAD" 2>/dev/null || echo "")
          if [ "$MERGE_BASE_4B" = "$BACKUP_GIT_HEAD" ]; then
            ANCESTRY="target_ahead"   # backup is an ancestor → target is newer
          elif [ "$MERGE_BASE_4B" = "$TARGET_HEAD" ]; then
            ANCESTRY="backup_ahead"   # target is an ancestor → backup is newer
          else
            ANCESTRY="diverged"
          fi
        else
          echo "  Backup HEAD not reachable in target repo — cannot determine ancestry."
        fi
      fi
    else
      echo "  No git_head.txt in backup — cannot determine ancestry."
    fi
    echo "  Ancestry: $ANCESTRY"

    while IFS= read -r dst_file; do
      rel="${dst_file#/agent/}"
      src_file="$RESTORE_DIR/agent/$rel"
      [ -f "$src_file" ] || continue
      diff -q "$src_file" "$dst_file" > /dev/null 2>&1 && continue  # identical, skip

      IS_TRACKED=0
      git -C /agent ls-files --error-unmatch -- "$rel" >/dev/null 2>&1 && IS_TRACKED=1

      case "$ANCESTRY" in
        target_ahead)
          if [ $IS_TRACKED -eq 1 ]; then
            # Target has newer commits — keep target, do not apply older backup content
            printf 'KEPT_TARGET  (target ahead of backup): %s\n' "$rel" >> "$REVIEW_4B_LOG"
            AUTO_KEPT_4B=$((AUTO_KEPT_4B + 1))
          else
            # Untracked file, ancestry known but can't confirm recency → manual review
            printf 'REVIEW_NEEDED (untracked, target_ahead): %s\n' "$rel" >> "$REVIEW_4B_LOG"
            NEEDS_HUMAN_4B=$((NEEDS_HUMAN_4B + 1))
          fi
          ;;
        backup_ahead)
          # Backup has newer commits — apply backup version to bring target forward
          if cp "$src_file" "$dst_file" 2>/dev/null; then
            printf 'APPLIED_BACKUP (backup ahead of target): %s\n' "$rel" >> "$REVIEW_4B_LOG"
            AUTO_APPLIED_4B=$((AUTO_APPLIED_4B + 1))
          else
            printf 'REVIEW_NEEDED (cp failed): %s\n' "$rel" >> "$REVIEW_4B_LOG"
            NEEDS_HUMAN_4B=$((NEEDS_HUMAN_4B + 1))
          fi
          ;;
        *)
          # diverged or unknown — keep target (safer default), flag for manual review
          printf 'REVIEW_NEEDED (%s): %s\n' "$ANCESTRY" "$rel" >> "$REVIEW_4B_LOG"
          NEEDS_HUMAN_4B=$((NEEDS_HUMAN_4B + 1))
          ;;
      esac
    done < "$SKIPPED_LOG"

    echo "  Kept target (target ahead):    $AUTO_KEPT_4B file(s)"
    echo "  Applied backup (backup ahead): $AUTO_APPLIED_4B file(s)"
    if [ $NEEDS_HUMAN_4B -gt 0 ]; then
      echo "  Needs manual review: $NEEDS_HUMAN_4B file(s) — see $REVIEW_4B_LOG"
      echo "  Follow Phase 4b in skills/full-backup-and-migrate/SKILL.md to resolve."
    fi
  fi
fi

# ── Phase 4.5: Restore Caddy live config ──────────────────────────────────────
echo ""
echo "[4.5/6] Restoring Caddy live config ..."
CADDY_JSON="$RESTORE_DIR/caddy_config.json"
if [ -f "$CADDY_JSON" ]; then
  if curl -fsS -X POST http://localhost:2019/load \
       -H 'Content-Type: application/json' \
       --data-binary "@$CADDY_JSON"; then
    echo "  Caddy live config restored from backup."
  else
    echo "  WARNING: failed to POST caddy_config.json to :2019/load — restore manually."
  fi
else
  echo "  No caddy_config.json in backup — Caddy keeps its current (bootstrap) config."
fi

# ── Phase 5: Self-test, services, cleanup ─────────────────────────────────────
echo ""
echo "[5/6] Verification ..."

# Run self-test BEFORE moving the zip / removing RESTORE_DIR so that a failure
# leaves the restore artifacts in place for re-runs (--tail-only).
SELF_TEST_RC=0
if [ $SKIP_SELF_TEST -eq 0 ]; then
  echo "  Running self_test.py ..."
  # Capture rc directly (do NOT use `if ! cmd; then rc=$?` — $? is the
  # negated test result, not the command's true exit code).
  (cd /agent && uv run python scripts/self_test.py) || SELF_TEST_RC=$?
  if [ $SELF_TEST_RC -ne 0 ]; then
    echo "  WARNING: self_test.py exited $SELF_TEST_RC"
  fi
  if curl -fsS http://localhost:8080/app/ -o /dev/null; then
    echo "  Gateway OK (http://localhost:8080/app/)"
  else
    echo "  WARNING: gateway probe failed at http://localhost:8080/app/"
  fi
else
  echo "  --skip-self-test set; skipping self_test.py + gateway probe."
fi

echo ""
echo "[6/6] Starting auto-start services ..."
# Venv was already synced by Phase 2b (which always runs in non-tail mode).
# In --tail-only mode the venv is assumed correct from a prior full run.
uv run python /agent/scripts/service_manager.py auto-start || true
uv run python /agent/scripts/service_manager.py health || true

# Only clean up + relocate the backup zip if everything succeeded. Keeping
# RESTORE_DIR around on failure lets the agent inspect or re-run --tail-only.
if [ $SELF_TEST_RC -eq 0 ]; then
  rm -rf "$RESTORE_DIR"
  echo "  Removed $RESTORE_DIR"
  if [ "$(dirname "$BACKUP_ZIP")" != "/agent/backup" ]; then
    mkdir -p /agent/backup
    mv "$BACKUP_ZIP" /agent/backup/
    echo "  Moved backup zip → /agent/backup/$(basename "$BACKUP_ZIP")"
  fi
else
  echo "  Leaving $RESTORE_DIR and $BACKUP_ZIP in place for re-runs."
fi

echo ""
echo "── Summary ───────────────────────────────────────────────────────────────"
if [ -n "${MANUAL_REVIEW_LOG:-}" ] && [ -s "$MANUAL_REVIEW_LOG" ]; then
  echo "  Phase 4a conflicts (skipped, reverted to HEAD): $(wc -l < "$MANUAL_REVIEW_LOG" | tr -d ' ') file(s)"
  echo "    → $MANUAL_REVIEW_LOG"
fi
if [ -n "${UNTRACKED_LOG:-}" ] && [ -s "$UNTRACKED_LOG" ]; then
  echo "  Phase 4a untracked-but-divergent: $(wc -l < "$UNTRACKED_LOG" | tr -d ' ') file(s)"
  echo "    → $UNTRACKED_LOG"
fi
if [ "${SELF_TEST_RC:-0}" -ne 0 ]; then
  echo "  Self-test exit code: $SELF_TEST_RC"
fi
echo "──────────────────────────────────────────────────────────────────────────"

if [ $SELF_TEST_RC -ne 0 ]; then
  echo "Restore finished WITH WARNINGS (self_test rc=$SELF_TEST_RC). Investigate before declaring migration done."
  exit 1
fi
echo "Restore complete."

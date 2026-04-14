---
name: claw-update-dna
description: Fetch and merge the latest changes from the claw-dex/claw-dna.git remote into the local branch, automatically resolving conflicts. Use when the user asks to pull agent updates, sync with claw dna git remote, update agent DNA or similar.
---

# claw-update-dna

Pull the latest changes from the `origin` remote and integrate them into the current local branch, resolving all conflicts autonomously.

**Remote:** the current `origin` remote configured in git (or `https://github.com/claw-dex/claw-dna.git` if no remote is configured)
**DNA Branch:** use the current branch name for the `{dna-branch-name}` placeholder

## Step-by-Step Process

### Step 1: Assess Current State

```bash
git status
git remote -v
git branch -a
```

Check for:

- **Uncommitted changes** (staged or unstaged)
- **Untracked files**
- **Current branch** and its tracking remote
- **Ahead/behind status** relative to remote

If can't determine current branch name, must pause and ask user to provide the name of the branch to pull from remote (DNA branch name)

### Step 2: Stash Local Changes (if any)

If `git status` shows modified or untracked files, stash everything before pulling:

```bash
git stash --include-untracked -m "Auto-stash before pull"
```

> **Edge case — `workspace/` busy warning:**
> The `workspace/` directory is a mounted volume and cannot be removed by git stash.
> A warning like `failed to remove workspace/: Device or resource busy` is **safe to ignore** — the stash still succeeds for all other files.

> **Edge case — stash partially fails:**
> If stash fails entirely (e.g., permission errors), fall back to committing the changes on a temporary branch:
>
> ```bash
> git checkout -b temp-local-changes
> git add -A && git commit -m "temp: save local changes before pull"
> git checkout {dna-branch-name}
> ```

> **Edge case — stash succeeds but tracked files remain dirty:**
> Some files may remain modified after stash (e.g., files in mounted volumes). Clean them manually before rebase:
>
> ```bash
> git diff --name-only
> git checkout -- <file1> <file2> ...
> ```
>
> These changes are safe to discard from the working tree because they are already saved in the stash.

### Step 3: Fetch from Remote

```bash
git fetch origin
```

Check if there are new commits:

```bash
git log --oneline HEAD..origin/{dna-branch-name}
```

If no new commits, skip to **Step 6** (restore stash and exit — already up to date).

### Step 4: Integrate Remote Changes

Choose the integration strategy based on the situation.

First, check how many local commits diverge from the remote:

```bash
git rev-list --count origin/{dna-branch-name}..HEAD
```

#### Strategy A: Rebase (preferred — clean linear history)

```bash
git rebase origin/{dna-branch-name}
```

Use rebase when:

- Local branch has **10 or fewer** commits ahead of remote
- You want a clean, linear commit history
- No shared/pushed commits that others depend on

#### Strategy B: Merge (preferred when heavily diverged)

```bash
git pull origin {dna-branch-name}
```

Use merge when:

- Local branch has **more than 10 commits** ahead of remote (rebase on large divergence is risky and produces excessive conflict rounds)
- Rebase fails with complex conflicts
- Local commits have already been pushed and shared
- You prefer an explicit merge commit for traceability

#### Strategy C: Fast-forward only (no local commits)

```bash
git pull --ff-only origin {dna-branch-name}
```

Use when the local branch has zero commits ahead of remote (just behind).

### Step 5: Resolve Conflicts

#### 5a: No conflicts

If rebase/merge completes cleanly, proceed to **Step 6**.

#### 5b: Rebase conflicts

When `git rebase` stops on a conflict:

```bash
# 1. See which files conflict
git status

# 2. For each conflicted file, inspect and resolve
#    - Read the file to see conflict markers (<<<<<<< / ======= / >>>>>>>)
#    - Edit the file to keep the correct version
#    - Use `git show HEAD:<file>` (local) and `git show REBASE_HEAD:<file>` (remote) to compare

# 3. After resolving each file
git add <resolved-file>

# 4. Continue the rebase
git rebase --continue

# 5. If a commit becomes empty after resolution
git rebase --skip
```

> **Edge case — rebase becomes too complex (many conflicts across commits):**
> Abort and fall back to merge:
>
> ```bash
> git rebase --abort
> git merge origin/{dna-branch-name}
> ```

#### 5c: Merge conflicts

When `git merge` reports conflicts:

```bash
# 1. See which files conflict
git diff --name-only --diff-filter=U

# 2. For each conflicted file, resolve the conflict markers
#    - Prefer LOCAL changes for: memory/, messages/, workspace/ (runtime state)
#    - Prefer REMOTE changes for: constitution.md, system.md, bootstrap.sh (immutable upstream)
#    - Manually merge for: server.py, app/*.py, scripts/*.py, pyproject.toml (code that diverges)

# 3. Stage resolved files
git add <resolved-file>
```

# 4. Complete the merge

At end of merge, **commit all staged changes** with a detailed, structured commit message.

Get the current cycle number from `state.json` (`cycle_number` field + 1, since it stores the last *completed* cycle) and include it in the commit message. The commit message must be long and descriptive — explain *what* changed, *why* the resolution was chosen. Use multi-line commit messages:

```bash
git commit -m "$(cat <<'EOF'
#<cycle_number> merge(origin/{dna-branch-name}): <summary of upstream changes>

Source: origin/{dna-branch-name} DNA update
Conflicts: <number of files with conflicts, or "none">

Changes merged:
- <detailed description of each upstream change incorporated>
- <what files were modified/created from upstream and why>
- <a summary of all conflicts occurred and how they were resolved>

Resolution strategy: 
- <file path>: <REMOTE or LOCAL or MANUAL MERGE> (the choice is based on 5d priority table, if no table rule applies, explain the reasoning behind the choice)

EOF
)"
```

#### 5d: Conflict resolution priorities

| File / Directory | Prefer | Reason |
|---|---|---|
| `constitution.md`, `system.md` | **REMOTE** | Immutable upstream files — always take the latest |
| `bootstrap.sh`, `heartbeat.sh` | **REMOTE** | Infrastructure managed upstream |
| `app/commands_tab.py` | **REMOTE** | Protected file — must not be locally modified |
| `memory/` | **LOCAL** | Runtime state — local is the source of truth |
| `messages/*.json` | **LOCAL** | Active message queues — never overwrite |
| `server.py`, `app/*.py` | **MANUAL MERGE** | May have both local improvements and upstream updates for the portal |
| `scripts/*.py` | **MANUAL MERGE** | Could have local additions and upstream fixes |
| `pyproject.toml` | **MANUAL MERGE** | Merge dependency lists from both sides |
| `prompts/*.md` | **REMOTE** | Prompt templates are upstream-managed |
| `AGENTS.md` | **MANUAL MERGE** | Has local customizations + upstream structure changes |
| `Dockerfile` | **REMOTE** | Build definition is upstream-managed |
| `skills/` | **MANUAL MERGE** | May have local skills + upstream skill updates |
| `workspace/` | **LOCAL** | User work — never overwrite |

#### 5e: Binary or large file conflicts

```bash
# Accept local version
git checkout --ours <file> && git add <file>

# Accept remote version
git checkout --theirs <file> && git add <file>
```

### Step 6: Restore Stashed Changes

```bash
git stash pop
```

> **Edge case — stash pop conflicts:**
> If the stash conflicts with the newly merged code:
>
> ```bash
> # Drop the conflicting stash pop
> git checkout -- .
> # Apply stash as a patch instead, resolving selectively
> git stash show -p | git apply --3way
> # Or manually inspect and apply
> git stash show -p > /tmp/stash.patch
> # Review the patch, apply relevant parts manually
> git stash drop
> ```

> **Edge case — stash was on a temp branch (from Step 2 fallback):**
>
> ```bash
> git cherry-pick temp-local-changes --no-commit
> git branch -D temp-local-changes
> ```

### Step 7: Post-Pull Verification

```bash
# 1. Confirm branch state
git log --oneline -5
git status

# 2. Check what changed — this informs Steps 8 and 9
git diff HEAD@{1}..HEAD --name-only

# 3. If seed/install.sh changed → proceed to Step 8
# 4. If pyproject.toml changed → proceed to Step 9

# 5. If portal files changed, verify portal health
curl -s http://localhost:8081/app/_stcore/health

# 6. If portal is broken, trigger a restart
bash scripts/server_restart.sh --verify
```

### Step 8: Install New OS-Level Dependencies (if needed)

If `seed/install.sh` was modified by the pull, new OS-level dependencies have been introduced and must be installed.

**Detection:**

```bash
# Check if seed/install.sh was changed in the incoming commits
git diff HEAD@{1}..HEAD --name-only | grep -q 'seed/install.sh'
```

If the file was changed:

```bash
# Re-execute the install script to pick up new dependencies
chmod +x seed/install.sh
bash seed/install.sh
```

> **Why re-run the entire script:** `install.sh` is designed to be idempotent — already-installed tools will be skipped or quickly verified. Re-running the full script is simpler and safer than trying to extract and execute only the new portions.

> **Edge case — partial failure:** If the script fails midway, inspect the output to identify which dependency failed. Fix the issue (e.g., network, permissions) and re-run. Already-installed dependencies will not be affected.

### Step 9: Sync Python Dependencies (if needed)

If `pyproject.toml` was modified (either by the pull or in the stash):

```bash
uv sync
```

This is **mandatory** per constitution — failing to sync after pyproject.toml changes will cause import errors.

## Quick Reference

```bash
# Full update in one shot (happy path, no local changes, ≤10 local commits)
git fetch origin && git rebase origin/{dna-branch-name}

# Full update when >10 local commits diverge (use merge instead of rebase)
git fetch origin && git pull origin {dna-branch-name}

# Full update with stash (has local changes, ≤10 local commits)
git stash --include-untracked -m "Auto-stash before pull"
git fetch origin
git rebase origin/{dna-branch-name}
git stash pop

# Nuclear option — discard ALL local uncommitted changes and force-sync
# (ONLY if user explicitly requests it)
git fetch origin
git reset --hard origin/{dna-branch-name}
```

## Common Edge Cases Summary

| Situation | Resolution |
|---|---|
| No remote changes | `git fetch` shows nothing new — exit early, restore stash |
| Clean rebase (≤10 local commits) | All commits replay without conflict — happy path |
| >10 local commits diverged | Skip rebase, use merge directly to avoid conflict cascades |
| Rebase conflict on 1-2 files | Resolve manually, `git add`, `git rebase --continue` |
| Rebase conflict cascade (many commits) | `git rebase --abort`, fall back to `git merge` |
| Merge conflict | Resolve using priority table above, `git add`, `git commit` |
| Stash pop conflict | Apply stash as patch (`git stash show -p \| git apply --3way`) |
| `workspace/` busy warning | Ignore — mounted volume, stash still works |
| Dirty tracked files won't stash | `git checkout -- <file>` to discard, or commit to temp branch |
| Stash succeeds but files remain dirty | `git checkout -- <files>` — they're saved in the stash already |
| `cannot rebase: unstaged changes` | Clean remaining dirty files with `git checkout --` before rebase |
| Portal broken after pull | `bash scripts/server_restart.sh --verify` |
| `seed/install.sh` changed | Re-run `bash seed/install.sh` to install new OS-level dependencies |
| `pyproject.toml` changed | Run `uv sync` immediately |
| Diverged history (force-push on remote) | `git fetch origin && git reset --hard origin/{dna-branch-name}` (destructive — confirm with user) |
| Authentication failure on fetch | Check GitHub credentials / SSH keys — may need human intervention |

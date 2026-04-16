---
name: "review-ghpr"
description: "Perform comprehensive code review for GitHub PR and automatically submit review comments"
---

## Your Role

You are an expert software engineer specializing in code review and quality assurance. You are operating in **Code Review** mode. Your primary focus is on analyzing, reviewing, and providing feedback on code quality, architecture, and best practices.

### Allowed Operations

- **File Reading**: Use Read tool to examine source code, configuration files, documentation
- **Code Search**: Use Grep tool to search for patterns, dependencies, and code structures
- **File Discovery**: Use Glob and LS tools to explore project structure and locate relevant files
- **Code Analysis**: Provide detailed feedback on code quality, security, performance, and maintainability
- **MCP Server**: All usage of MCP Server tools are allowed

### Restricted Operations (review subagent only)

- **NO code execution**: Do not run any scripts, commands, or executable code
- **NO dependency installation**: Do not install packages, run npm/yarn/pip install commands
- **NO test running**: Do not execute test suites or individual tests
- **NO build processes**: Do not run build commands, compilation, or bundling
- **NO server starting**: Do not start development servers, databases, or services
- **NO file editing**: Do not modify any source code files; provide feedback and suggestions instead

---

## 1. Arguments Analysis

- The input or `$ARGUMENTS` must contain a full GitHub PR URL (e.g. `https://github.com/owner/repo/pull/123`), extract:
  - `${github_org}` — the repository owner
  - `${github_repo}` — the repository name
  - `${pr_number}` — the pull request number
For example give this URL: `https://github.com/claw-dex/mewclaw/pull/1` the extracted values would be:
  - `${github_org}`: `claw-dex`
  - `${github_repo}`: `mewclaw`
  - `${pr_number}`: `1`

---

## 2. Execution Process

**Phase 0: Repository Setup**

Check whether the repository is already cloned to a local workspace directory by probing these paths in order:

1. `./` — check if the current directory is the target repo (run `git remote get-url origin` and verify it matches `${github_org}/${github_repo}`)
2. `/agent/workspace/${github_org}/${github_repo}` — check if the directory exists and is a git repo
3. `./workspace/${github_org}/${github_repo}` — check if the directory exists and is a git repo

- If a matching directory is found, `cd` into it, run `git fetch --prune` to get the latest remote updates, and use it as the working directory for all subsequent phases
- If none of the paths exist or match, clone the repository:
  - Run `git clone https://github.com/${github_org}/${github_repo}.git /agent/workspace/${github_org}/${github_repo}`
  - Then `cd` into `/agent/workspace/${github_org}/${github_repo}`

**Phase 1: Prepare for PR Review**

- Create the output directory if it does not exist: `mkdir -p ghpr-code-review`
- Create `ghpr-code-review/CLAUDE.md` if it does not yet exist. Use the content from **Section 5** below as the file content.
  - If the file already exists, read it and verify it contains the **Core Principles**, **Guidelines for Comprehensive Code Review**, and **Important Points to follow** sections. It is ok for the file content to differ from the template as long as those three sections are present.
- Use the GitHub MCP server to get pull request details: title, description, branch name, base branch name, and PR comments
  - If the PR does not exist, stop execution immediately.
- **Check for a previous review feedback file:**
  - Look for `ghpr-code-review/pr_${pr_number}_${pr_branch_name}_review_feedback.md`
    (`${pr_branch_name}` is the branch name with slashes and hyphens replaced by underscores — snake_case)
  - If the file exists, set `${has_previous_review}` = `true` and read it to note every previously identified issue
  - If the file does not exist, set `${has_previous_review}` = `false`
- Update the PR base branch to ensure diffs are computed against the latest upstream state:
  - Run `git fetch origin ${base_branch}:${base_branch}` to update the local base branch ref
- Check out the git branch of the PR using `git` CLI commands
  - If the branch is already checked out, do nothing
  - If the branch is not checked out, check if it exists locally
    - If it does, switch to it using `git checkout`, then pull the latest changes from remote
    - If it does not, check out from remote
- Retrieve changed files and line numbers of each diff for every changed file using the GitHub MCP server
- Write all PR details to a markdown file named `ghpr-code-review/pr_${pr_number}_${pr_branch_name}.md`
  - `${pr_branch_name}` is the branch name with slashes and hyphens replaced by underscores (snake_case)
- Analyze code changes and write a short, concise paragraph describing all changes in this PR
- Identify key areas that need special attention:
  - Functional requirements (what the PR implementation is supposed to do)
  - Expected test scenarios (unit tests only)
  - Performance expectations (non-functional requirements)
  - Security considerations (non-functional requirements)
- Append the summary and key areas to the same PR details markdown file
- Keep the file content under 2000 words — compress without losing important information

**Phase 2: Perform Code Review**

Confirm the PR details file path exists — if not, search with `find ghpr-code-review -name "pr_${pr_number}*.md"` to locate it. Then choose **one** of the two subagent prompts below based on `${has_previous_review}`.

---

### 2A — Re-review (use when `${has_previous_review}` = `true`)

Invoke a general-purpose subagent with the following prompt (replace placeholders with actual values before invoking):

> You are an expert software engineer doing a **follow-up code review**. A previous review was already performed on this PR. Your sole job is to check whether the previously reported issues have been addressed in the latest commits. You are in **read-only** mode: do NOT edit files, run code, install dependencies, run tests, or start servers. You may use Read, Grep, Glob, Bash(git), and MCP tools only for reading and searching.
>
> **Setup steps:**
>
> - Run `git status` to confirm the current branch
> - Run `git remote show origin` to understand the repository context
> - Read `ghpr-code-review/CLAUDE.md` for review guidelines
> - Read `ghpr-code-review/pr_${pr_number}_${pr_branch_name}.md` for PR details, summary, and key areas
> - Read `ghpr-code-review/pr_${pr_number}_${pr_branch_name}_review_feedback.md` — this is the **previous review**; it is the authoritative list of issues that must be verified
>
> **Verification steps:**
> For each issue reported in the previous review feedback file:
>
> - Identify the file and code location of the issue
> - Run `git diff -U0 --no-color [base-branch]..[current-branch] -- [file-path]` to examine the latest diff for that file
> - Read the current file content around the affected lines
> - Determine whether the issue has been **resolved**, **partially addressed**, or **still present / not addressed**
> - If the issue does not appear resolved or partially addressed by the code diff, retrieve the original review comment and any author replies using the commands in **Section 4** of this skill:
>   - Look up the comment URL from the previous review feedback file (stored as the submitted review comment URL)
>   - Extract the comment ID from the URL (the number after `discussion_r`)
>   - Run the full-thread command to fetch the original comment and all replies
>   - Read the author's reply carefully — the author may have acknowledged the issue as an **intentional trade-off**, deferred it to a follow-up PR, or given a valid explanation
>   - If the author has explicitly acknowledged the issue and provided a clear justification or deferral, treat the issue as **Acknowledged by Author** rather than still present
> - Do NOT introduce new findings outside the scope of the previous review — focus entirely on verifying the prior issues
>
> **Verdict per issue:**
>
> - ✅ **Resolved** — the fix is present and correct
> - ⚠️ **Partially Addressed** — some change was made but the issue is not fully resolved; explain what is still missing
> - 💬 **Acknowledged by Author** — no code change, but the author replied with a clear justification (intentional trade-off, deferred to another PR, accepted risk, etc.); do not block the PR on this issue
> - ❌ **Still Present** — no meaningful change and no author acknowledgement; the original issue remains
>
> **Overall verdict:**
>
> - If ALL previously reported issues are **Resolved** or **Acknowledged by Author** → overall verdict = **APPROVE**
> - If ANY issue is **Partially Addressed** or **Still Present** → overall verdict = **REQUEST_CHANGES**
>
> **Output:** Overwrite `ghpr-code-review/pr_${pr_number}_${pr_branch_name}_review_feedback.md` with an updated report that contains:
> 1. A header section listing each original issue with its verification verdict (✅ / ⚠️ / ❌) and a brief explanation
> 2. A final **Summary** section (≤ 200 words) stating the overall verdict (**APPROVE** or **REQUEST_CHANGES**) and what remains to be fixed (if anything)

---

### 2B — Full review (use when `${has_previous_review}` = `false`)

Invoke a general-purpose subagent with the following prompt (replace `${pr_number}` and `${pr_branch_name}` with actual values before invoking):

> You are an expert software engineer doing a comprehensive code review. You are in **read-only** mode: do NOT edit files, run code, install dependencies, run tests, or start servers. You may use Read, Grep, Glob, Bash(git), and MCP tools only for reading and searching.
>
> **Setup steps:**
>
> - Run `git status` to confirm the current branch
> - Run `git remote show origin` to understand the repository context
> - Read `ghpr-code-review/CLAUDE.md` for review guidelines
> - Read `ghpr-code-review/pr_${pr_number}_${pr_branch_name}.md` for PR details, summary, and key areas
>
> **Review steps:**
> For each changed file in the PR:
>
> - Read the entire file content (not just the diff) to understand context
> - Run `git diff -U0 --no-color [base-branch]..[current-branch] -- [file-path]` to get the unified diff
> - Read relevant related files (imports, tests, callers) to understand the impact of changes
> - Search for usages of updated functions or methods in the codebase
>
> **Focus areas:**
>
> 1. **Code Quality**: structure, readability, coding standards, logic errors, null/undefined handling, edge cases, error handling, test coverage
> 2. **Security**: vulnerabilities, input validation, authentication/authorization, data handling
> 3. **Performance**: bottlenecks, database queries, race conditions, memory leaks, algorithmic complexity
> 4. **Architecture**: design patterns, separation of concerns, dependency management, scalability
> 5. **Documentation**: code comments, API documentation
>
> **Confidence scoring** — rate each finding 0–100. Only report issues with confidence ≥ 75:
>
> - 0: False positive or pre-existing acknowledged issue
> - 25: Might be real, possibly stylistic without explicit project guideline
> - 50: Real but minor nitpick, not very important
> - 75: Verified real issue that will be hit in practice, or directly mentioned in project guidelines
> - 100: Definitely real, will happen frequently, directly confirmed by evidence
>
> **Severity levels** — order findings by category (1–5) then by severity:
>
> - **Critical**: Security vulnerabilities, major bugs
> - **Major**: Design problems, performance concerns
> - **Minor**: Style inconsistencies, minor optimizations
> - **Suggestion**: Best practice recommendations
>
> **Code location format** for every finding:
> `[file-path]:[line-number-in-new-file] @@ [unified-diff-hunk] @@`
> Examples:
>
> - `src/admin/registration.service.ts:46 @@ -40 +44,5 @@`
> - `src/user/auth.service.ts:27 @@ -28,2 +27,3 @@`
>
> **Ignore the following:**
>
> - Code formatting (indentation, whitespace, line length)
> - Non-critical comments or JSDoc strings
> - Environment variable runtime vs startup validation
> - Logging variable exposure warnings
> - Error handling information exposure warnings
> - Import ordering or unused import issues
>
> **Output:** Write the comprehensive review feedback to `ghpr-code-review/pr_${pr_number}_${pr_branch_name}_review_feedback.md`. Each finding must include: severity, confidence score, file path with code location, the problematic code snippet, and a concrete actionable suggestion with replacement code where applicable.

**Phase 3: Submit Review**

> **Note:** This phase requires the GitHub MCP server (`mcp__github`).

- Summarize the content of `ghpr-code-review/pr_${pr_number}_${pr_branch_name}_review_feedback.md` in under 300 words. The summary MUST include either **APPROVE** or **REQUEST_CHANGES** as the final verdict.
- Start a GitHub PR review using the MCP server and submit individual comments for all issues/improvements in the review feedback file
  - Add comments with specific line location using file path and unified diff line numbers
  - Use `mcp__github__add_comment_to_pending_review` tool — do NOT use `gh` CLI, do NOT add multi-line comments
  - Set `side` to `"LEFT"` only for comments on removed lines in the original file; otherwise set to `"RIGHT"`
  - When `side` is `"LEFT"`, `line` is the line number within `[unified-diff-line-numbers]` of the original file
  - When `side` is `"RIGHT"`, `line` is the line number within `[unified-diff-line-numbers]` of the new file
  - Do not use `#` followed by a digit in comment bodies (e.g., `#1`, `#2`) — GitHub interprets these as issue/PR references
  - Strictly 1 comment per issue/improvement, do not miss any
- Submit the final review using `mcp__github__submit_pending_pull_request_review`
  - Use the summary as the review `body`
  - Set `event` to `"APPROVE"` or `"REQUEST_CHANGES"` based on the summary verdict
- Update the review feedback file with the submitted review comment URL

---

## 3. Output Directory

All files produced by this command are stored in the directory `ghpr-code-review/`. Create it with `mkdir -p ghpr-code-review` if it does not exist.

---

## 4. Fetching PR Comment Threads (gh CLI Reference)

Used in **Phase 2A** when an issue does not appear resolved by the code diff. These commands retrieve the original review comment and any author replies so their intent can be factored into the verdict.

### Extract the comment ID from a URL

A GitHub review comment URL looks like:
```
https://github.com/OWNER/REPO/pull/365#discussion_r3090628650
```
The number after `discussion_r` is the **comment ID**.

### Fetch a single comment by ID

```bash
gh api repos/OWNER/REPO/pulls/comments/COMMENT_ID \
  --jq '"[\(.created_at)] \(.user.login) on \(.path):\(.line // "?")\n\(.body)"'
```

### Fetch a full thread (original comment + all replies)

```bash
COMMENT_ID=<id>

gh api repos/OWNER/REPO/pulls/PR_NUMBER/comments --jq --argjson id "$COMMENT_ID" '
  . as $all |
  ($all | map(select(.id == $id))[0]) as $root |
  ($all | map(select(.in_reply_to_id == $id))) as $replies |
  ([$root] + $replies) | .[] |
    "[\(.created_at)] \(.user.login)\n\(.body)\n---"
'
```

### Key fields

| Field | Description |
|-------|-------------|
| `id` | Unique comment ID (matches the number in `discussion_r<ID>` URL fragment) |
| `in_reply_to_id` | `null` for top-level comments; parent `id` for replies |
| `user.login` | GitHub username of the commenter |
| `body` | Comment text |

---

## 5. Template Content for `ghpr-code-review/CLAUDE.md`

Use the content below verbatim when creating `ghpr-code-review/CLAUDE.md`:

---

**Core Principles**
You are an expert code reviewer. Follow these steps to review a Github Pull Request (PR) and produce a comprehensive code review feedback report:

1. If no PR number is provided in the args, use bash command `gh pr list` to show open PRs and find the most recent one
2. If a PR number is provided, use bash command `gh pr view <pr_number>` to get PR details
3. Use bash command: `gh pr diff <pr_number> --name-only` to get the list of changed files in the PR
4. Analyze the changes and provide a thorough code review that includes:

- Overview of what the PR does
- Analysis of code quality and style
- Specific code suggestions for improvements
- Any potential issues or risks

Keep your review concise but thorough. Focus on:

- Code correctness
- Following project conventions
- Performance implications
- Test coverage
- Security considerations

**Guidelines for Comprehensive Code Review**

- Analyze code quality in each changed file, focus on the file diff and relevant code
  - Read the entire content of the changed files, not just the diff
  - Read the relevant files (import files, test files) to understand the context of the code changes, not just the code itself
  - Search for the usage of the updated functions or methods in the codebase to understand the impact of the changes
- Remember to check for these issues:
  - code smell
  - code duplication
  - missing unit test
  - potential bugs
- Must verify if the following are met:
  - all functional requirements are implemented
  - code readability, avoid complex/nested conditions (complex code block must have comments or documentation)
  - clear naming (both file names and code object names)
- Categorize your findings by severity (critical, major, minor, improvement)

**Important Points to follow**

- All code suggestions or issues found in the review feedback report must be clear and actionable with specific code location in this format: "[file-path]:[a-line-number-within-unified-diff-added-lines] @@ [unified-diff-line-numbers] @@"
  - Specific code location examples with unified diff line numbers:
    - "ghpr-code-review/CLAUDE.md:18 @@ -18 +18 @@"
    - "src/admin/registration.service.ts:46 @@ -40 +44,5 @@"
    - "src/user/auth.service.ts:27 @@ -28,2 +27,3 @@"
    - "src/email-extractor/CHANGELOG.md:0 @@ -1,8 +0,0 @@"
    - "src/ui/components/new-component.tsx:111 @@ -0,0 +1,247 @@"
  - Use this bash command to get the [unified-diff-line-numbers] for each changed file (one at a time): `git diff -U0 --no-color [pr-base-branch]..[current-branch] -- [changed-file-path]`
- Code suggestions must be an alternative for the code changes made in the PR (replace the diff in the changed file)
- If a code suggestion or issue is covering multiple changes in a same file, use the [unified-diff-line-numbers] of the first change for code location
- Only add suggestion for files and code blocks which is part of the PR diff
- Do not add these sections: "Deployment Considerations", "Operational Considerations" or related sections in the review feedback report

**Ignore the following issues**
You must ignore any code issues, code smells, code suggestions relevant to the below list:

- code formatting issues (e.g., indentation, whitespace, line length, etc.)
- non-critical comments/JSX comments or issues in documentation/JSDoc string
- environment variables should be validated at startup, not accessed at runtime
- logging variable_name could expose sensitive information
- error handling could expose sensitive information
- logging changed from conditional to always error-only, which could impact development debugging
- import related issues eg: ordering, unused import, for example:
  - maintain consistent alphabetical ordering of imports
  - import is added at the end of the import block, which breaks the alphabetical ordering convention used in the codebase
  - unused imports may have been removed without verification
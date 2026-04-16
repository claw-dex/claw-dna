---
name: "gh-cli-pr-comment"
description: "Fetch and display inline PR review comments and threads using the gh CLI"
---

## Overview

Use the `gh` CLI to fetch inline pull request review comments from GitHub. Supports filtering by user, showing replies, fetching a single comment by URL, and displaying full threads.

---

## Usage

The input or `$ARGUMENTS` must contain a full GitHub PR URL or enough information to extract:
- `${github_org}` — repository owner
- `${github_repo}` — repository name
- `${pr_number}` — pull request number

For example, given `https://github.com/claw-dex/mewclaw/pull/1`:
- `${github_org}`: `claw-dex`
- `${github_repo}`: `mewclaw`
- `${pr_number}`: `1`

---

## Commands

### 1. List all inline comments from a specific user

```bash
gh api repos/OWNER/REPO/pulls/PR_NUMBER/comments \
  --jq '[.[] | select(.user.login == "USERNAME")] | sort_by(.created_at) | .[] |
    "[\(.created_at)] \(.path):\(.line // "?")\n\(.body)\n---"'
```

**Example:**
```bash
gh api repos/open-fabric/slice-zapp-authz-service/pulls/365/comments \
  --jq '[.[] | select(.user.login == "api-openfabric")] | sort_by(.created_at) | .[] |
    "[\(.created_at)] \(.path):\(.line // "?")\n\(.body)\n---"'
```

---

### 2. Show replies to a specific user's comments

The `in_reply_to_id` field links a reply back to the parent comment's `id`.

```bash
gh api repos/OWNER/REPO/pulls/PR_NUMBER/comments --jq '
  . as $all |
  ($all | map(select(.user.login == "USERNAME")) | map(.id)) as $targetIds |
  $all
  | map(select(
      .in_reply_to_id != null and
      ((.in_reply_to_id | tostring) as $rid | $targetIds | map(tostring) | contains([$rid]))
    ))
  | .[] | "[\(.created_at)] \(.user.login) replied on \(.path):\(.line // "?")\n\(.body)\n---"
'
```

---

### 3. Show a specific comment by URL

A GitHub comment URL looks like:
```
https://github.com/OWNER/REPO/pull/365#discussion_r3090628650
```
The number after `discussion_r` is the **comment ID**.

```bash
gh api repos/OWNER/REPO/pulls/comments/COMMENT_ID \
  --jq '"[\(.created_at)] \(.user.login) on \(.path):\(.line // "?")\n\(.body)"'
```

Then fetch its replies:
```bash
gh api repos/OWNER/REPO/pulls/PR_NUMBER/comments \
  --jq '[.[] | select(.in_reply_to_id == COMMENT_ID)] |
    .[] | "[\(.created_at)] \(.user.login)\n\(.body)\n---"'
```

---

### 4. Show a full thread (comment + all replies)

```bash
COMMENT_ID=3090628650

gh api repos/OWNER/REPO/pulls/PR_NUMBER/comments --jq --argjson id "$COMMENT_ID" '
  . as $all |
  ($all | map(select(.id == $id))[0]) as $root |
  ($all | map(select(.in_reply_to_id == $id))) as $replies |
  ([$root] + $replies) | .[] |
    "[\(.created_at)] \(.user.login)\n\(.body)\n---"
'
```

---

## Key Fields

| Field | Description |
|-------|-------------|
| `id` | Unique comment ID (matches the number in `discussion_r<ID>` URL fragment) |
| `in_reply_to_id` | `null` for top-level comments; parent `id` for replies |
| `user.login` | GitHub username of the commenter |
| `path` | File path the comment is on |
| `line` | Line number in the new file (`null` for file-level comments) |
| `body` | Comment text |

---

## Submitting Comments

### 5. Post a new PR-level (general discussion) comment

```bash
gh api repos/OWNER/REPO/issues/PR_NUMBER/comments \
  --method POST \
  -f body="Your comment text here" \
  --jq '{id, html_url}'
```

**Example:**
```bash
gh api repos/open-fabric/slice-zapp-authz-service/issues/365/comments \
  --method POST \
  -f body="LGTM overall, left a few inline notes." \
  --jq '{id, html_url}'
```

> Use the **issues** endpoint for general discussion comments (the PR "Conversation" tab). These are not tied to a file or line.

---

### 6. Reply to an existing inline comment thread

Replying requires only the `in_reply_to` field (the parent comment's ID) plus the `body`. GitHub resolves the file/line context automatically from the parent.

```bash
gh api repos/OWNER/REPO/pulls/PR_NUMBER/comments \
  --method POST \
  -f body="Your reply text here" \
  -F in_reply_to=COMMENT_ID \
  --jq '{id, html_url}'
```

**Example** — reply to comment `3049641084` on PR 365:
```bash
gh api repos/open-fabric/slice-zapp-authz-service/pulls/365/comments \
  --method POST \
  -f body="Agreed, addressed in the latest commit." \
  -F in_reply_to=3049641084 \
  --jq '{id, html_url}'
```

> The comment ID is the number after `discussion_r` in the GitHub URL fragment (e.g. `#discussion_r3049641084`).

---

### 7. Delete a comment

```bash
# Delete an inline review comment
gh api repos/OWNER/REPO/pulls/comments/COMMENT_ID --method DELETE

# Delete a PR-level (issue) comment
gh api repos/OWNER/REPO/issues/comments/COMMENT_ID --method DELETE
```

---

## Endpoints Reference

| What | Endpoint |
|------|----------|
| Inline review comments (on code lines) — read/write | `GET/POST /repos/OWNER/REPO/pulls/PR/comments` |
| Single inline comment — read/delete | `GET/DELETE /repos/OWNER/REPO/pulls/comments/COMMENT_ID` |
| PR-level comments (general discussion tab) — read/write | `GET/POST /repos/OWNER/REPO/issues/PR/comments` |
| Single PR-level comment — read/delete | `GET/DELETE /repos/OWNER/REPO/issues/comments/COMMENT_ID` |

Inline review comments and their replies all live under the **pulls** endpoint. Replies are regular inline comments with `in_reply_to_id` set.

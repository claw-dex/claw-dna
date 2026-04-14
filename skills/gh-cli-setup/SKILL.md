---
name: gh-cli-setup
description: Set up and configure the GitHub CLI (gh) with a Personal Access Token (PAT). Use when users need to authenticate gh with a token, switch accounts, check auth status, log in to GitHub Enterprise, or troubleshoot gh auth issues. Triggers on "set up gh cli", "authenticate github cli", "gh login with token", "gh auth", "setup PAT for gh", or any GitHub CLI authentication task.
---

# GitHub CLI Setup

Configure and manage GitHub CLI (`gh`) authentication using a Personal Access Token (PAT).

## Quick Reference

| Task | Command |
|------|---------|
| Login with PAT (KeePass) | `uv run python /agent/scripts/keepass.py get "GITHUB_TOKEN_1" \| gh auth login --with-token` |
| Login to GHE | `gh auth login --hostname enterprise.internal` |
| Check status | `gh auth status` |
| Switch account | `gh auth switch --hostname github.com --user <username>` |
| Logout | `gh auth logout` |

## Workflows

### 1. Authenticate with a PAT Token

Retrieve the token from KeePass and pipe it directly — the token never touches the filesystem or shell history:

```bash
# Retrieve from KeePass and pipe to gh (recommended)
uv run python /agent/scripts/keepass.py get "GITHUB_TOKEN_1" | gh auth login --with-token

# For CI: use an env var set from KeePass at runtime
export GITHUB_TOKEN="$(uv run python /agent/scripts/keepass.py --json get "GITHUB_TOKEN_1" | jq -r '.password')"
# gh picks up GITHUB_TOKEN automatically — no auth login needed
gh repo list
```

Store the token in KeePass first if not already there:
```bash
uv run python /agent/scripts/keepass.py store \
  --title "GITHUB_TOKEN_1" \
  --username "your-gh-username" \
  --password "ghp_yourTokenHere" \
  --group "API Keys"
```

After login, verify it worked:
```bash
gh auth status
```

### 2. Login to GitHub Enterprise

```bash
gh auth login --hostname enterprise.internal
```

If using a PAT with GHE, pull from KeePass:
```bash
uv run python /agent/scripts/keepass.py get "GitHub Enterprise Token" | \
  gh auth login --hostname enterprise.internal --with-token
```

### 3. Check Authentication Status

```bash
gh auth status
```

Shows active account, hostname, token scopes, and expiry (if set).

For all configured hosts:
```bash
gh auth status --show-token
```

### 4. Multiple GitHub Accounts

Tokens for multiple accounts are stored in KeePass using the naming convention `GITHUB_TOKEN_1`, `GITHUB_TOKEN_2`, `GITHUB_TOKEN_3`, etc.

**Store each account's token:**
```bash
uv run python /agent/scripts/keepass.py store \
  --title "GITHUB_TOKEN_1" --username "user-one" --password "ghp_token1" --group "API Keys"

uv run python /agent/scripts/keepass.py store \
  --title "GITHUB_TOKEN_2" --username "user-two" --password "ghp_token2" --group "API Keys"

uv run python /agent/scripts/keepass.py store \
  --title "GITHUB_TOKEN_3" --username "org-bot" --password "ghp_token3" --group "API Keys"
```

**Login with a specific account:**
```bash
# Login account 1
uv run python /agent/scripts/keepass.py get "GITHUB_TOKEN_1" | gh auth login --with-token

# Login account 2 (gh allows multiple authenticated accounts)
uv run python /agent/scripts/keepass.py get "GITHUB_TOKEN_2" | gh auth login --with-token
```

**Set active account for the current shell session:**
```bash
# Export a specific account's token as the active one
export GITHUB_TOKEN="$(uv run python /agent/scripts/keepass.py --json get "GITHUB_TOKEN_2" | jq -r '.password')"
```

**List all credentials to check how many GITHUB_TOKEN entries exist:**
```bash
uv run python /agent/scripts/keepass.py --json list
```
Parse the output to find entries with titles matching `GITHUB_TOKEN_*` and determine the next available number before storing a new one.

### 6. Switch Between Accounts

```bash
# Switch on github.com
gh auth switch --user <username>

# Switch on a specific hostname
gh auth switch --hostname enterprise.internal --user <username>
```

List all authenticated accounts:
```bash
gh auth status
```

### 7. Set Token via Environment Variable (CI/CD)

```bash
export GITHUB_TOKEN="ghp_yourToken"
# gh will automatically pick this up — no auth login needed
gh repo list
```

## Creating a PAT

If the user needs a PAT, guide them:

1. Go to **GitHub → Settings → Developer settings → Personal access tokens**
2. Choose **Tokens (classic)** or **Fine-grained tokens**
3. Select required scopes:
   - `repo` — full repo access
   - `read:org` — org membership (needed for org repos)
   - `workflow` — manage GitHub Actions
4. Copy the token immediately — it won't be shown again

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `gh: command not found` | gh not installed | `brew install gh` (macOS) or see [cli.github.com](https://cli.github.com) |
| `error connecting to api.github.com` | Network/proxy issue | Check `HTTPS_PROXY` env var |
| `HTTP 401: Bad credentials` | Invalid or expired token | Generate a new PAT |
| `HTTP 403: Forbidden` | Token missing required scope | Re-generate with correct scopes |
| `Your token has expired` | Token past expiry date | Rotate the token on GitHub |
| `must authenticate with classic token` | Fine-grained token used where not supported | Use a classic PAT |

## Secure Token Handling Tips

- Store PATs in KeePass (`keepass.py store`) — never in plaintext files or hardcoded in scripts
- Pipe from KeePass directly: `keepass.py get "GITHUB_TOKEN_1" | gh auth login --with-token` — token never hits disk or history
- For CI, export `GITHUB_TOKEN` at runtime from KeePass rather than baking it into config
- Revoke compromised tokens immediately at **GitHub → Settings → Developer settings → Personal access tokens**

## Platform Notes

- **macOS**: `gh` stores credentials in the system keychain by default
- **Linux**: credentials stored in `~/.config/gh/hosts.yml`
- **CI environments**: prefer `GITHUB_TOKEN` env var; avoid interactive login

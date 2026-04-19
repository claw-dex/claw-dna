#!/usr/bin/env python3
"""GitHub pull-request webhook handler for webhook_receiver.

Supports multiple GitHub orgs. Each org gets its own endpoint:

    https://<public-url>/webhook/github/<org_name>/pull_request

The org_name is extracted from the URL path and used to look up
per-org configuration (guideline prompt) and the per-org HMAC secret
from KeePass.

Responsibilities:
- POST /github/<org>/pull_request: receive a GitHub pull-request webhook.
  Reviewable PR actions (opened, reopened, ready_for_review,
  review_requested) on non-draft PRs from configured orgs queue a review
  goal in inbox.json. For review_requested the agent's GitHub user
  (username from the GITHUB_TOKEN_1 KeePass credential) must appear in
  pull_request.requested_reviewers — otherwise the request was for some
  other reviewer and is written as an informative event only.
  All other deliveries (other actions including synchronize, drafts,
  ignored events, missing fields, unconfigured orgs, payload/URL org
  mismatch) are acknowledged (200) and written to the inbox as
  informative events so operators retain visibility.
- Paths that don't match /github/<org>/pull_request → 404.
- GET/other methods → 405.

Configuration lives in
    /agent/memory/github_pull_request_handler_state.json

Schema — keyed by org name (string):
    {
      "anthropics": {
        "guideline_prompt_path": "/agent/prompts/code_review_anthropics.md"
      }
    }

State is re-read on every request, so org config can be tuned without
restarting webhook_receiver.

HMAC-SHA256 signature verification is enabled per org when the KeePass
credential GITHUB_WEBHOOK_SECRET_<org> is present. If absent, requests
for that org are accepted unverified. Orgs listed in the state file get
a warning at startup; orgs not in the state file get a one-time warning
on their first delivery.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from shared import read_json_file, write_to_inbox
from webhook.github_webhook_common import (
    GithubWebhookHandlerBase,
    KEEPASS_AGENT_TOKEN_TITLE,
    get_agent_github_user,
)

# --- Paths ---
BASE = Path("/agent")
STATE_FILE = BASE / "memory" / "github_pull_request_handler_state.json"

# PR actions worth reviewing. Others (labeled, assigned, closed, synchronize,
# …) get an informative inbox event but no goal. `review_requested` is
# gated further: the requested reviewer must be the GitHub user this agent
# acts as (see KEEPASS_AGENT_TOKEN_TITLE).
REVIEWABLE_ACTIONS = {"opened", "reopened", "ready_for_review", "review_requested"}


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _parse_org_state(raw: dict) -> dict:
    """Normalise a single org config block from the state file."""
    prompt_path = raw.get("guideline_prompt_path")
    if not isinstance(prompt_path, str) or not prompt_path:
        prompt_path = None
    return {"guideline_prompt_path": prompt_path}


def load_org_state(org: str, log) -> dict | None:
    """Return parsed state for a specific org, or None if not configured.

    Re-reads STATE_FILE on every call so operators can onboard a new org
    by editing the JSON without restarting the receiver.
    """
    data = read_json_file(STATE_FILE, default={})
    if not isinstance(data, dict):
        log.warning(f"{STATE_FILE.name}: expected a JSON object, treating as empty")
        return None

    raw = data.get(org)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        log.warning(f"Org {org!r} state is not a JSON object, treating as unconfigured")
        return None

    return _parse_org_state(raw)


def list_configured_orgs() -> list[str]:
    """Return org names present in the state file."""
    data = read_json_file(STATE_FILE, default={})
    if not isinstance(data, dict):
        return []
    return [k for k in data if isinstance(data[k], dict)]


# ---------------------------------------------------------------------------
# Handler class
# ---------------------------------------------------------------------------


class GithubPullRequestHandler(GithubWebhookHandlerBase):
    """Webhook sub-handler for GitHub pull-request events (multi-org).

    Registered at path_prefix = "/github". Handles requests matching:
        /github/<org>/pull_request
    """

    EVENT_NAME = "pull_request"
    SOURCE = "github_pull_request_webhook"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self.log.info("Starting GitHub pull-request handler (multi-org)...")
        orgs = list_configured_orgs()
        if orgs:
            for org in orgs:
                self._load_secret(org)
                secret_status = (
                    "signature verification enabled"
                    if self._secrets.get(org)
                    else "NO secret — verification DISABLED"
                )
                state = load_org_state(org, self.log) or {}
                guideline = state.get("guideline_prompt_path") or "<no guideline>"
                self.log.info(
                    f"  Org {org}: guideline={guideline}, {secret_status}"
                )
        else:
            self.log.warning(
                f"No orgs configured in {STATE_FILE.name}. "
                "All deliveries will be acknowledged but produce informative "
                "events only (no review goals queued). Add an entry keyed by "
                "org name to enable review queueing."
            )
        self.log.info(
            f"GitHub {self.EVENT_NAME} handler ready — "
            f"path pattern: /webhook/github/<org>/{self.EVENT_NAME}"
        )

    # ------------------------------------------------------------------
    # Event processing
    # ------------------------------------------------------------------

    def _process_event(self, url_org: str, event: str, payload: dict) -> None:
        action = str(payload.get("action") or "")
        pr = payload.get("pull_request") or {}
        repo = payload.get("repository") or {}
        payload_org = (repo.get("owner") or {}).get("login") or ""
        repo_name = repo.get("name") or ""
        pr_number = pr.get("number")
        pr_url = pr.get("html_url") or ""
        is_draft = bool(pr.get("draft"))

        # Non-PR events reaching this URL are a GitHub-side misconfiguration;
        # still record them so operators see the mismatch.
        if event and event != self.EVENT_NAME:
            self._informative(
                url_org, event, action, payload,
                note=f"X-GitHub-Event {event!r} does not match handler event {self.EVENT_NAME!r}",
            )
            return

        # URL/payload org consistency — reject cross-tenant webhook misrouting.
        if payload_org and payload_org.lower() != url_org.lower():
            self.log.warning(
                f"Org {url_org}: payload org {payload_org!r} does not match URL "
                "— writing informative"
            )
            self._informative(
                url_org, event, action, payload,
                note=f"payload org {payload_org!r} does not match URL org {url_org!r}",
            )
            return

        if action not in REVIEWABLE_ACTIONS:
            self._informative(
                url_org, event, action, payload,
                note=f"action {action!r} not in REVIEWABLE_ACTIONS",
                log_detail=f"ignoring PR action {action!r} for {repo_name}#{pr_number}",
            )
            return

        # review_requested is only actionable when the GitHub user this
        # agent acts as is among the requested reviewers — otherwise the
        # review request is for somebody else and we just log it for
        # visibility.
        if action == "review_requested":
            agent_user = get_agent_github_user(self.log)
            reviewers = pr.get("requested_reviewers") or []
            reviewer_logins = [
                (r.get("login") or "")
                for r in reviewers
                if isinstance(r, dict)
            ]
            if not agent_user:
                self._informative(
                    url_org, event, action, payload,
                    note=(
                        f"review_requested but {KEEPASS_AGENT_TOKEN_TITLE} "
                        "username is unavailable — cannot determine if this "
                        "agent is the intended reviewer"
                    ),
                )
                return
            if not any(
                login.lower() == agent_user.lower()
                for login in reviewer_logins
            ):
                self._informative(
                    url_org, event, action, payload,
                    note=(
                        f"review_requested for reviewers {reviewer_logins!r}; "
                        f"agent user {agent_user!r} is not among them"
                    ),
                    log_detail=(
                        f"review_requested on {repo_name}#{pr_number} for "
                        f"{reviewer_logins!r} does not include agent user "
                        f"{agent_user!r}"
                    ),
                )
                return

        if is_draft and action != "ready_for_review":
            self._informative(
                url_org, event, action, payload,
                note="draft PR (skipped until ready_for_review)",
                log_detail=f"skipping draft PR {repo_name}#{pr_number}",
            )
            return

        if not pr_url or pr_number is None:
            self._informative(
                url_org, event, action, payload,
                note=f"payload missing required fields (url={pr_url!r}, number={pr_number!r})",
                log_detail=(
                    f"PR payload missing required fields "
                    f"(url={pr_url!r}, number={pr_number!r})"
                ),
                log_level="warning",
            )
            return

        state = load_org_state(url_org, self.log)
        if not state:
            self._informative(
                url_org, event, action, payload,
                note=f"org {url_org!r} not configured in {STATE_FILE.name}",
                log_detail=(
                    f"no entry in {STATE_FILE.name} for {repo_name}#{pr_number}"
                ),
            )
            return

        guideline_path = state.get("guideline_prompt_path")
        if not guideline_path:
            self._informative(
                url_org, event, action, payload,
                note=f"org {url_org!r} has no guideline_prompt_path configured",
                log_detail=(
                    f"no guideline_prompt_path configured for "
                    f"{repo_name}#{pr_number}"
                ),
            )
            return

        self._queue_review_goal(
            url_org, action, repo_name, pr_number, pr_url, guideline_path
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _informative(
        self,
        url_org: str,
        event: str,
        action: str,
        payload: dict,
        *,
        note: str,
        log_detail: str | None = None,
        log_level: str = "info",
    ) -> None:
        """Log + write one informative inbox entry.

        ``log_detail`` is an optional human-readable sentence for the
        service log (full context for operators); ``note`` is the
        shorter note embedded in the inbox entry.
        """
        log_msg = f"Org {url_org}: " + (log_detail or note) + " — writing informative"
        getattr(self.log, log_level)(log_msg)
        self._write_informative_event(url_org, event, action, payload, note=note)

    def _queue_review_goal(
        self,
        url_org: str,
        action: str,
        repo_name: str,
        pr_number,
        pr_url: str,
        guideline_path: str,
    ) -> None:
        content = (
            f"Run the /review-ghpr skill on {pr_url}. "
            f"Use the code review guideline at {guideline_path}. "
            f"(triggered by GitHub webhook event={action} "
            f"org={url_org} repo={repo_name} pr=#{pr_number})"
        )
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "type": "goal",
            "content": content,
            "timestamp": now,
            "received_at": now,
            "source": self.SOURCE,
            "org": url_org,
            "priority": 3,
        }
        if write_to_inbox([item]):
            self.log.info(
                f"Org {url_org}: queued review goal for "
                f"{repo_name}#{pr_number} ({action})"
            )
        else:
            self.log.error(
                f"Org {url_org}: failed to queue review goal for "
                f"{repo_name}#{pr_number}"
            )

#!/usr/bin/env python3
"""GitHub ``pull_request_review_comment`` webhook handler.

Informative-only: every verified delivery is acknowledged and recorded
in the inbox as a ``type: "event"`` item. No goals are queued.

URL pattern: ``/webhook/github/<org>/pull_request_review_comment``.

Actions surfaced by GitHub for this event: ``created``, ``edited``,
``deleted``. See
https://docs.github.com/en/webhooks/webhook-events-and-payloads#pull_request_review_comment

Self-authored comments (comments posted by the GitHub user the agent
acts as, per the ``GITHUB_TOKEN_1`` KeePass credential) are flagged in
the inbox note so downstream readers can tell at a glance whether a
comment is the agent's own output or came from a human reviewer.
"""

from webhook.github_webhook_common import (
    InformativeGithubWebhookHandler,
    get_agent_github_user,
)


class GithubPullRequestReviewCommentHandler(InformativeGithubWebhookHandler):
    EVENT_NAME = "pull_request_review_comment"
    SOURCE = "github_pull_request_review_comment_webhook"

    def _note_for(self, payload: dict) -> str:
        comment = payload.get("comment") or {}
        user = (comment.get("user") or {}).get("login") or "?"
        path = comment.get("path") or "?"
        # `line` is null for resolved/outdated comments; fall back to
        # original_line so the summary is never just "?".
        line = comment.get("line") or comment.get("original_line") or "?"

        agent_user = get_agent_github_user(self.log)
        self_flag = ""
        if agent_user and user and user.lower() == agent_user.lower():
            self_flag = " [SELF — comment authored by this agent]"

        return f"review comment by {user} on {path}:{line}{self_flag}"

#!/usr/bin/env python3
"""GitHub ``pull_request_review_thread`` webhook handler.

Informative-only: every verified delivery is acknowledged and recorded
in the inbox as a ``type: "event"`` item. No goals are queued.

URL pattern: ``/webhook/github/<org>/pull_request_review_thread``.

Actions surfaced by GitHub for this event: ``resolved``, ``unresolved``.
See
https://docs.github.com/en/webhooks/webhook-events-and-payloads#pull_request_review_thread

Self-triggered thread actions (threads resolved/unresolved by the
GitHub user the agent acts as, per the ``GITHUB_TOKEN_1`` KeePass
credential) are flagged in the inbox note, mirroring the SELF tagging
done by the review-comment handler.
"""

from webhook.github_webhook_common import (
    InformativeGithubWebhookHandler,
    get_agent_github_user,
)


class GithubPullRequestReviewThreadHandler(InformativeGithubWebhookHandler):
    EVENT_NAME = "pull_request_review_thread"
    SOURCE = "github_pull_request_review_thread_webhook"

    def _note_for(self, payload: dict) -> str:
        sender = (payload.get("sender") or {}).get("login") or "?"
        thread = payload.get("thread") or {}
        comments = thread.get("comments") or []

        agent_user = get_agent_github_user(self.log)
        self_flag = ""
        if agent_user and sender and sender.lower() == agent_user.lower():
            self_flag = " [SELF — action performed by this agent]"

        return (
            f"thread action by {sender} "
            f"({len(comments)} comments in thread){self_flag}"
        )

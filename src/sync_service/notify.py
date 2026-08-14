"""Notify: every halt/error from run_mapping is routed here -- printed as a
source-commit comment, and posted to Slack too if SLACK_WEBHOOK_URL is set (see
slack.py). Neither is required; Slack has no toggle of its own since it carries
no authority over the sync, only whether the env var is set.
"""
from __future__ import annotations

import os

from . import slack


def comment_on_commit(commit_sha: str, body: str) -> str:
    short = commit_sha[:12]
    # GITHUB_REPOSITORY ("owner/repo") is set automatically on every GitHub
    # Actions run -- absent locally/in tests, where a bare sha is all there is
    # to show anyway.
    repo = os.environ.get("GITHUB_REPOSITORY")
    commit_url = f"https://github.com/{repo}/commit/{commit_sha}" if repo else None

    if commit_url:
        message = f"[sync-service] comment on {commit_url}:\n{body}"
        # A plain URL, not Slack's <url|text> syntax, here -- this is what gets
        # printed to the Action's own log, which auto-linkifies a bare URL but
        # would show mrkdwn's angle brackets as literal text.
        print(message)
        # Slack gets the shorter, more readable link text via its own <url|text>
        # mrkdwn syntax instead of a full URL in the message body.
        slack.post(f"[sync-service] comment on <{commit_url}|{short}>:\n{body}")
    else:
        message = f"[sync-service] comment on {short}:\n{body}"
        print(message)
        slack.post(message)

    return message


def pr_opened(project_label: str, title: str, detail: str) -> None:
    """Slack-only -- an opened PR is already visible on GitHub, this just flags it.
    `detail` is publish.open_pr's result: a real PR URL (rendered as a Slack link,
    `<url|text>`, not Markdown) or dry-run preview text (no URL to link, so just
    `title` is posted)."""
    if detail.startswith("[dry-run"):
        slack.post(f"[{project_label}] PR opened (dry-run): {title}")
    else:
        slack.post(f"[{project_label}] PR opened: <{detail}|{title}>")

"""Notify: every halt/error from run_direction is routed here -- printed as a
source-commit comment, and posted to Slack too if SLACK_WEBHOOK_URL is set (see
slack.py). Neither is required; Slack has no toggle of its own since it carries
no authority over the sync, only whether the env var is set.
"""
from __future__ import annotations

from . import slack


def comment_on_commit(commit_sha: str, body: str) -> str:
    message = f"[sync-service] comment on {commit_sha[:12]}:\n{body}"
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

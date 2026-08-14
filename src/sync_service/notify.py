"""Notify: every halt/error from run_direction is routed here. Prints a comment on the
source commit, via the production repo's own read-only GITHUB_TOKEN (no extra
permission needed), and -- if SLACK_WEBHOOK_URL is configured -- the same message
posted to Slack too (see slack.py). Neither is required: Slack posting is
best-effort and never raises, and the GitHub comment stays print-only in this demo.

Slack is a second channel, not a replacement: it doesn't get its own toggle in the
mapping config, since it carries no authority over the sync at all (unlike
llm_pr/llm_safety_review) -- whether it fires is controlled entirely by whether
SLACK_WEBHOOK_URL is set, the same pattern OPENAI_API_KEY already uses elsewhere.
"""
from __future__ import annotations

from . import slack


def comment_on_commit(commit_sha: str, body: str) -> str:
    message = f"[sync-service] comment on {commit_sha[:12]}:\n{body}"
    print(message)
    slack.post(message)
    return message


def pr_opened(project_label: str, title: str, detail: str) -> None:
    """Slack-only -- a successfully opened PR is already visible on GitHub itself
    (reviewers watching that repo see it there); this just saves someone from
    having to notice it. `project_label` is cli.py's `project_label` -- either
    `SyncConfig.project_name` (e.g. "Prod") if set, or the mechanical
    `label:mapping_key` fallback if not; see cli.py's run_direction. `detail` is
    whatever publish.open_pr produced: a real PR URL (rendered as a Slack link --
    `<url|text>` is Slack's own mrkdwn syntax, not `[text](url)`, which Slack does
    not render as a link at all), or the dry-run preview text if no remote is
    configured (no real URL to link to, so `title` alone is posted instead)."""
    if detail.startswith("[dry-run"):
        slack.post(f"[{project_label}] PR opened (dry-run): {title}")
    else:
        slack.post(f"[{project_label}] PR opened: <{detail}|{title}>")

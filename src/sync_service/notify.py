"""Notify — architecture.md §7 failure matrix. Every halt/error is a comment on the
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


def pr_opened(label: str, mapping_key: str, detail: str) -> None:
    """Slack-only -- a successfully opened PR is already visible on GitHub itself
    (reviewers watching that repo see it there); this just saves someone from
    having to notice it. `detail` is whatever publish.open_pr produced: a real PR
    URL, or the dry-run preview text if no remote is configured."""
    slack.post(f"[{label}:{mapping_key}] PR opened: {detail}")

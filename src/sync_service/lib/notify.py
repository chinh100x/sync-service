"""Notify: every halt/error from run_mapping is routed here -- printed to the
Action's own log (always), posted as a real GitHub comment on the source
commit (if a token with write access to the source repo is available), and
posted to Slack too if SLACK_WEBHOOK_URL is set (see slack.py). None of the
three is required; the log print always happens regardless, so a halt is
never *only* visible to someone who happened to have Slack configured.
"""

from __future__ import annotations

import os
import subprocess

from . import slack


def _post_github_comment(repo: str, commit_sha: str, body: str, token: str) -> None:
    """Best-effort: this never raises. A missing `gh`, an insufficient token
    scope (posting a commit comment needs `contents: write` on the *source*
    repo -- a different token from the one that pushes to the OSS side), or
    any other API failure just means this one channel didn't deliver -- the
    print above already happened regardless, so the halt is never silent
    even if this fails."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits/{commit_sha}/comments", "-f", f"body={body}"],
        env={**os.environ, "GH_TOKEN": token},
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"[sync-service] GitHub commit comment failed: {proc.stderr.strip()}")


def comment_on_commit(commit_sha: str, body: str, token: str | None = None) -> str:
    short = commit_sha[:12]
    # GITHUB_REPOSITORY ("owner/repo") is set automatically on every GitHub
    # Actions run -- absent locally/in tests, where a bare sha is all there is
    # to show anyway.
    repo = os.environ.get("GITHUB_REPOSITORY")
    commit_url = f"https://github.com/{repo}/commit/{commit_sha}" if repo else None
    # Fenced so the body renders as a code block everywhere it's posted --
    # GitHub comments and Slack mrkdwn both use the same ``` syntax, so one
    # format serves every channel. Callers should never add their own nested
    # fence inside `body` (e.g. around raw command output) -- Slack/GitHub
    # don't render nested triple-backtick fences correctly; let this one
    # outer fence cover the whole message instead.
    fenced_body = f"```\n{body}\n```"

    if commit_url:
        message = f"[sync-service] comment on {commit_url}:\n{fenced_body}"
        # A plain URL, not Slack's <url|text> syntax, here -- this is what gets
        # printed to the Action's own log, which auto-linkifies a bare URL but
        # would show mrkdwn's angle brackets as literal text.
        print(message)
        # Slack gets the shorter, more readable link text via its own <url|text>
        # mrkdwn syntax instead of a full URL in the message body.
        slack.post(f"[sync-service] comment on <{commit_url}|{short}>:\n{fenced_body}")
    else:
        message = f"[sync-service] comment on {short}:\n{fenced_body}"
        print(message)
        slack.post(message)

    # No token, or no way to know which repo to post to (e.g. running locally) --
    # the print + Slack above already covered the notification; this is a bonus
    # channel, not the only one.
    if token and repo:
        _post_github_comment(repo, commit_sha, fenced_body, token)

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

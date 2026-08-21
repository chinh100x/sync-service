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
    repo = os.environ.get("GITHUB_REPOSITORY")
    commit_url = f"https://github.com/{repo}/commit/{commit_sha}" if repo else None
    # Slack-only -- Slack's message list mashes multi-line text together
    # without it. The Action log print, the returned message, and the real
    # GitHub commit comment all get the body as-is: a GitHub comment already
    # renders as its own distinct block, so fencing it there is just noise.
    fenced_body = f"```\n{body}\n```"

    if commit_url:
        message = f"[sync-service] comment on {commit_url}:\n{body}"
        print(message)
        slack.post(f"[sync-service] comment on <{commit_url}|{short}>:\n{fenced_body}")
    else:
        message = f"[sync-service] comment on {short}:\n{body}"
        print(message)
        slack.post(f"[sync-service] comment on {short}:\n{fenced_body}")

    if token and repo:
        _post_github_comment(repo, commit_sha, body, token)

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

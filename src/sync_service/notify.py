"""Notify — architecture.md §7 failure matrix. Every halt is a comment on the source
commit, via the production repo's own read-only GITHUB_TOKEN (no extra permission needed).

In this demo there's no real GitHub API call — the comment is printed and returned so
the CLI/tests can show exactly what would have been posted.
"""
from __future__ import annotations


def comment_on_commit(commit_sha: str, body: str) -> str:
    message = f"[sync-service] comment on {commit_sha[:12]}:\n{body}"
    print(message)
    return message

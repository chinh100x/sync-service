"""Publish — design.md §3 pass path. One branch/commit per mapping; PR opened via `gh`
when available, otherwise the PR body is printed (dry-run, e.g. this demo)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# Identity for the sync commits themselves — a CI runner has no git identity configured
# by default. Override via env if the org wants a different bot identity/name.
_COMMIT_NAME = os.environ.get("SYNC_SERVICE_COMMIT_NAME", "sync-service[bot]")
_COMMIT_EMAIL = os.environ.get("SYNC_SERVICE_COMMIT_EMAIL", "sync-service@users.noreply.github.com")
_GIT_ID = ["-c", f"user.name={_COMMIT_NAME}", "-c", f"user.email={_COMMIT_EMAIL}"]


def branch_name(namespace: str, head_sha: str) -> str:
    """namespace is the full branch prefix, e.g. `sync/portmon` or `reverse-sync/portmon`."""
    return f"{namespace}/{head_sha[:12]}"


def branch_exists(dest_repo: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=dest_repo,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def commit_to_branch(dest_repo: Path, branch: str, message: str) -> bool:
    """Assumes dest_repo's working tree already has base_branch checked out with the
    scrubbed changes pending (uncommitted). Moves them onto a new branch.

    Returns False (no branch left behind) if there was nothing to commit — the
    propagated content was already byte-identical to what's on the far side. With
    no manifest forcing a write every run, this is a real case, not just theoretical."""
    subprocess.run(["git", *_GIT_ID, "checkout", "-b", branch], cwd=dest_repo, check=True, capture_output=True)
    subprocess.run(["git", *_GIT_ID, "add", "-A"], cwd=dest_repo, check=True, capture_output=True)

    nothing_staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=dest_repo).returncode == 0
    if nothing_staged:
        subprocess.run(["git", *_GIT_ID, "checkout", "-"], cwd=dest_repo, check=True, capture_output=True)
        subprocess.run(["git", *_GIT_ID, "branch", "-D", branch], cwd=dest_repo, check=True, capture_output=True)
        return False

    subprocess.run(["git", *_GIT_ID, "commit", "-m", message], cwd=dest_repo, check=True, capture_output=True)
    return True


def discard_working_tree_changes(dest_repo: Path) -> None:
    subprocess.run(["git", "checkout", "--", "."], cwd=dest_repo, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=dest_repo, capture_output=True)


def open_pr(dest_repo: Path, branch: str, base_branch: str, title: str, body: str) -> str:
    """Returns a human-readable result. Uses `gh pr create` if a remote + gh are configured,
    otherwise returns the PR body as a dry-run description (this is the demo's path)."""
    has_gh = shutil.which("gh") is not None
    has_remote = subprocess.run(
        ["git", "remote"], cwd=dest_repo, capture_output=True, text=True
    ).stdout.strip() != ""

    if has_gh and has_remote:
        push = subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=dest_repo,
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            return f"git push failed: {push.stderr.strip()}"

        proc = subprocess.run(
            [
                "gh", "pr", "create",
                "--base", base_branch,
                "--head", branch,
                "--title", title,
                "--body", body,
            ],
            cwd=dest_repo,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        return f"gh pr create failed: {proc.stderr.strip()}"

    return (
        f"[dry-run: no remote configured] Would open PR {branch} -> {base_branch}\n"
        f"Title: {title}\n\n{body}"
    )

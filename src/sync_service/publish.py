"""Publish — design.md §3 pass path. One branch/commit per mapping; PR opened via `gh`
when available, otherwise the PR body is printed (dry-run, e.g. this demo)."""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _basic_auth_header(token: str) -> str:
    """A one-off `-c http.extraheader=...` value for a single git invocation — never
    written to any .git/config, unlike actions/checkout's persisted-credential mode."""
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {encoded}"

# Identity for the sync commits themselves — a CI runner has no git identity configured
# by default. Override via env if the org wants a different bot identity/name.
_COMMIT_NAME = os.environ.get("SYNC_SERVICE_COMMIT_NAME", "sync-service[bot]")
_COMMIT_EMAIL = os.environ.get("SYNC_SERVICE_COMMIT_EMAIL", "sync-service@users.noreply.github.com")
_GIT_ID = ["-c", f"user.name={_COMMIT_NAME}", "-c", f"user.email={_COMMIT_EMAIL}"]


def branch_name(namespace: str, head_sha: str) -> str:
    """namespace is the full branch prefix, e.g. `sync/portmon` or `reverse-sync/portmon`."""
    return f"{namespace}/{head_sha[:12]}"


def checkout_base(dest_repo: Path, base_branch: str) -> None:
    """Reset dest_repo to base_branch before processing a mapping. Required when a
    single run handles multiple mappings against the same far_repo checkout — without
    this, the second mapping's commit_to_branch() would branch off whatever the first
    mapping's branch left HEAD on, nesting one mapping's commit inside the other's PR
    instead of both branching independently off base_branch."""
    subprocess.run(["git", *_GIT_ID, "checkout", base_branch], cwd=dest_repo, check=True, capture_output=True)


def branch_exists(dest_repo: Path, branch: str) -> bool:
    """Checks both local and remote — a fresh Action runner's checkout has no local
    knowledge of a branch a previous run pushed, only main's own history. Without the
    remote check, idempotency silently stops working the moment this runs on CI instead
    of a long-lived local clone."""
    local = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=dest_repo,
        capture_output=True,
        text=True,
    )
    if local.returncode == 0:
        return True

    remote = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=dest_repo,
        capture_output=True,
        text=True,
    )
    return remote.returncode == 0


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


def reword_commit(dest_repo: Path, message: str) -> None:
    """Rewrites the message of the commit `commit_to_branch` just made, before it's
    ever pushed -- amending a not-yet-pushed local commit is ordinary git, not a
    rewrite of shared history. Used to swap the mechanical placeholder message for
    pr_writer.py's already-generated, already-safe title (see design-history.md's
    v14 note) -- this is NOT a reopening of v7's leak: the replacement text comes
    from the same far-side-only, already-scrubbed context the PR body already uses,
    never from anything production-side."""
    subprocess.run(["git", *_GIT_ID, "commit", "--amend", "-m", message], cwd=dest_repo, check=True, capture_output=True)


def discard_working_tree_changes(dest_repo: Path) -> None:
    subprocess.run(["git", "checkout", "--", "."], cwd=dest_repo, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=dest_repo, capture_output=True)


@dataclass
class PublishResult:
    success: bool
    message: str


def open_pr(dest_repo: Path, branch: str, base_branch: str, title: str, body: str, token: str | None = None) -> PublishResult:
    """Uses `gh pr create` if a remote is configured, otherwise returns the PR body as
    a dry-run description (this is the demo's path) -- dry-run counts as success, since
    nothing was supposed to happen for real.

    `git push` authenticates via an explicit, single-invocation `-c http.extraheader`
    built from `token` when one's given — never via a persisted checkout credential
    (action.yml sets `persist-credentials: false` for exactly this reason: a persisted
    credential sits in plaintext in .git/config, readable by anything with this
    checkout's cwd, including break_check's install/run commands). Only `gh pr create`
    additionally needs `GH_TOKEN` as an env var, scoped to just that one subprocess call.

    "No remote configured" and "remote configured but `gh` is missing" are deliberately
    different outcomes: the former is a legitimate local/demo scenario (dry-run,
    success); the latter means a real publish was intended and the environment can't do
    it — that must be a failure, not a silent dry-run reported as success."""
    has_remote = subprocess.run(
        ["git", "remote"], cwd=dest_repo, capture_output=True, text=True
    ).stdout.strip() != ""

    if not has_remote:
        return PublishResult(
            True,
            f"[dry-run: no remote configured] Would open PR {branch} -> {base_branch}\n"
            f"Title: {title}\n\n{body}",
        )

    if shutil.which("gh") is None:
        return PublishResult(False, "gh CLI not found, but a remote is configured — cannot publish")

    push_cmd = ["git"]
    if token:
        push_cmd += ["-c", f"http.extraheader={_basic_auth_header(token)}"]
    push_cmd += ["push", "-u", "origin", branch]
    push = subprocess.run(push_cmd, cwd=dest_repo, capture_output=True, text=True)
    if push.returncode != 0:
        return PublishResult(False, f"git push failed: {push.stderr.strip()}")

    gh_env = {**os.environ, "GH_TOKEN": token} if token else os.environ
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
        env=gh_env,
    )
    if proc.returncode == 0:
        return PublishResult(True, proc.stdout.strip())
    return PublishResult(False, f"gh pr create failed: {proc.stderr.strip()}")

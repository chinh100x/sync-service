"""Publish: commit the scrubbed content to a new branch, then open a PR for it. One
branch/commit per mapping; PR opened via `gh` when a remote is configured, otherwise
the PR body is printed instead (dry-run, e.g. this demo)."""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _basic_auth_header(token: str) -> str:
    """One-off `-c http.extraheader=...` for a single git invocation -- never
    written to .git/config, unlike a persisted checkout credential."""
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {encoded}"


def _git_id() -> list[str]:
    """Commit identity, read lazily (not a module constant) so cli.py can set a
    project-specific default via os.environ after this module is imported."""
    name = os.environ.get("SYNC_SERVICE_COMMIT_NAME", "sync-service[bot]")
    email = os.environ.get("SYNC_SERVICE_COMMIT_EMAIL", "sync-service@users.noreply.github.com")
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def branch_name(namespace: str, head_sha: str) -> str:
    """Temporary working branch, committed onto before the PR title (and therefore
    the final branch name) exists. Renamed by rename_branch once it does; never
    itself pushed. Idempotency doesn't depend on this name -- see already_synced."""
    return f"{namespace}/{head_sha[:7]}"


def _sync_ref(mapping_key: str, head_sha: str) -> str:
    return f"refs/sync-service/{mapping_key}/{head_sha}"


def already_synced(dest_repo: Path, mapping_key: str, head_sha: str) -> bool:
    """Whether (mapping, head_sha) was already proposed, via a dedicated ref
    recorded by record_synced -- not the branch name, which is a plain slug that
    can't double as an idempotency key."""
    ref = _sync_ref(mapping_key, head_sha)
    local = subprocess.run(["git", "show-ref", "--verify", "--quiet", ref], cwd=dest_repo)
    if local.returncode == 0:
        return True

    remote = subprocess.run(["git", "ls-remote", "origin", ref], cwd=dest_repo, capture_output=True, text=True)
    return bool(remote.stdout.strip())


def record_synced(dest_repo: Path, mapping_key: str, head_sha: str, token: str | None = None) -> None:
    """Marks (mapping, head_sha) synced. Call only after a real success, never on a
    halt/failure, so a failed run still gets retried. Remote push is best-effort --
    a failure here just risks a redundant PR later, never an incorrect skip."""
    ref = _sync_ref(mapping_key, head_sha)
    subprocess.run(["git", "update-ref", ref, "HEAD"], cwd=dest_repo, check=True, capture_output=True)

    has_remote = subprocess.run(["git", "remote"], cwd=dest_repo, capture_output=True, text=True).stdout.strip() != ""
    if not has_remote:
        return

    push_cmd = ["git"]
    if token:
        push_cmd += ["-c", f"http.extraheader={_basic_auth_header(token)}"]
    push_cmd += ["push", "origin", f"{ref}:{ref}"]
    subprocess.run(push_cmd, cwd=dest_repo, capture_output=True, text=True)


def slugify(text: str, max_length: int = 50) -> str:
    """Git-ref-safe branch fragment. Falls back to "change" if nothing
    alphanumeric survives, so an unusual title never yields an empty name."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "change"


def rename_branch(dest_repo: Path, new_name: str) -> None:
    """Renames the current, not-yet-pushed branch -- ordinary git, not a rewrite
    of shared history."""
    subprocess.run(["git", *_git_id(), "branch", "-m", new_name], cwd=dest_repo, check=True, capture_output=True)


def checkout_base(dest_repo: Path, base_branch: str) -> None:
    """Reset dest_repo to base_branch before processing a mapping -- otherwise a
    second mapping in the same run branches off the first mapping's commit
    instead of base_branch."""
    subprocess.run(["git", *_git_id(), "checkout", base_branch], cwd=dest_repo, check=True, capture_output=True)


def branch_exists(dest_repo: Path, branch: str) -> bool:
    """Checks local and remote -- a fresh CI checkout only knows main's history,
    not a branch a previous run pushed."""
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


def commit_to_branch(dest_repo: Path, branch: str, message: str, author: str | None = None) -> bool:
    """Commits dest_repo's pending working-tree changes onto a new branch.

    `author` credits the real near-side committer via git's Author field, kept
    distinct from the Committer field (`_git_id()`, the bot identity).

    Returns False (no branch left behind) if there was nothing to commit --
    happens when propagated content is already byte-identical to the far side."""
    subprocess.run(["git", *_git_id(), "checkout", "-b", branch], cwd=dest_repo, check=True, capture_output=True)
    subprocess.run(["git", *_git_id(), "add", "-A"], cwd=dest_repo, check=True, capture_output=True)

    nothing_staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=dest_repo).returncode == 0
    if nothing_staged:
        subprocess.run(["git", *_git_id(), "checkout", "-"], cwd=dest_repo, check=True, capture_output=True)
        subprocess.run(["git", *_git_id(), "branch", "-D", branch], cwd=dest_repo, check=True, capture_output=True)
        return False

    commit_cmd = ["git", *_git_id(), "commit", "-m", message]
    if author:
        commit_cmd += ["--author", author]
    subprocess.run(commit_cmd, cwd=dest_repo, check=True, capture_output=True)
    return True


def reword_commit(dest_repo: Path, message: str) -> None:
    """Rewrites commit_to_branch's placeholder message before it's ever pushed --
    an amend of a not-yet-pushed local commit, not a rewrite of shared history.
    Safe to swap in pr_writer's title: it's built only from already-scrubbed
    far-side context, never the raw (untrusted) production commit message."""
    subprocess.run(["git", *_git_id(), "commit", "--amend", "-m", message], cwd=dest_repo, check=True, capture_output=True)


def discard_working_tree_changes(dest_repo: Path) -> None:
    subprocess.run(["git", "checkout", "--", "."], cwd=dest_repo, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=dest_repo, capture_output=True)


@dataclass
class PublishResult:
    success: bool
    message: str


def open_pr(dest_repo: Path, branch: str, base_branch: str, title: str, body: str, token: str | None = None) -> PublishResult:
    """Opens a PR via `gh` if a remote is configured; otherwise prints the PR body
    as a dry-run description (this demo's path) -- dry-run counts as success.

    Pushes via a one-off `-c http.extraheader` built from `token`, never a
    persisted credential (which break_check's install/run commands could read).

    "No remote" (dry-run success) and "remote but no `gh`" (failure) are
    deliberately different outcomes -- the latter means a real publish was
    intended and the environment can't do it."""
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

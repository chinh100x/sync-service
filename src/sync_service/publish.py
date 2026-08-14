"""Publish — design.md §3 pass path. One branch/commit per mapping; PR opened via `gh`
when available, otherwise the PR body is printed (dry-run, e.g. this demo)."""
from __future__ import annotations

import base64
import os
import re
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
# by default. Read lazily (a function, not a frozen module-level constant) so cli.py
# can derive a project-specific default from SyncConfig.project_name and set it via
# os.environ *after* this module is already imported -- a constant evaluated once at
# import time would miss that. An explicit SYNC_SERVICE_COMMIT_NAME/_EMAIL always wins
# over any project_name-derived default (see cli.py's main()).
def _git_id() -> list[str]:
    name = os.environ.get("SYNC_SERVICE_COMMIT_NAME", "sync-service[bot]")
    email = os.environ.get("SYNC_SERVICE_COMMIT_EMAIL", "sync-service@users.noreply.github.com")
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def branch_name(namespace: str, head_sha: str) -> str:
    """namespace is the full branch prefix, e.g. `sync/portmon` or `reverse-sync/portmon`.
    This is the *temporary* working name used while committing -- cli.py renames it to
    a human-readable, title-derived name (see rename_branch) once the PR title is known,
    before anything is pushed. It also doubles as the idempotency prefix: the (mapping,
    head_sha) identity a re-run needs to recognize is fully captured here, regardless of
    what slug gets appended later -- see branch_exists_with_prefix. 7 chars -- git's own
    default abbreviation length -- rather than 12: shorter, still practically unique for
    a repo this size."""
    return f"{namespace}/{head_sha[:7]}"


def slugify(text: str, max_length: int = 50) -> str:
    """Git-ref-safe, human-readable fragment for a branch name -- lowercase,
    non-alphanumeric runs collapsed to single hyphens, capped at max_length. Falls
    back to "change" if nothing alphanumeric survives, so a branch name derived from
    unusual title text (all punctuation, non-Latin script, etc.) is never left empty
    or malformed."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "change"


def rename_branch(dest_repo: Path, new_name: str) -> None:
    """Renames the current (local, not-yet-pushed) branch -- swaps commit_to_branch's
    temporary sha-only working name for the final, title-derived name once the PR
    title is known. Ordinary git, same as reword_commit's amend: nothing here has
    been pushed yet, so this isn't a rewrite of shared history."""
    subprocess.run(["git", *_git_id(), "branch", "-m", new_name], cwd=dest_repo, check=True, capture_output=True)


def checkout_base(dest_repo: Path, base_branch: str) -> None:
    """Reset dest_repo to base_branch before processing a mapping. Required when a
    single run handles multiple mappings against the same far_repo checkout — without
    this, the second mapping's commit_to_branch() would branch off whatever the first
    mapping's branch left HEAD on, nesting one mapping's commit inside the other's PR
    instead of both branching independently off base_branch."""
    subprocess.run(["git", *_git_id(), "checkout", base_branch], cwd=dest_repo, check=True, capture_output=True)


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


def branch_exists_with_prefix(dest_repo: Path, prefix: str) -> bool:
    """Like branch_exists, but a glob-prefix match instead of an exact name -- needed
    once branch names carry a title-derived slug that isn't known until after the
    commit (and its generated title) already exist. `prefix` alone (e.g.
    "sync/portmon/e184f69") is the full (mapping, head_sha) idempotency key; whatever
    slug text a prior run appended after it is irrelevant to "has this already been
    proposed."""
    local = subprocess.run(
        ["git", "branch", "--list", f"{prefix}*"],
        cwd=dest_repo,
        capture_output=True,
        text=True,
    )
    if local.stdout.strip():
        return True

    remote = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", f"{prefix}*"],
        cwd=dest_repo,
        capture_output=True,
        text=True,
    )
    return bool(remote.stdout.strip())


def commit_to_branch(dest_repo: Path, branch: str, message: str) -> bool:
    """Assumes dest_repo's working tree already has base_branch checked out with the
    scrubbed changes pending (uncommitted). Moves them onto a new branch.

    Returns False (no branch left behind) if there was nothing to commit — the
    propagated content was already byte-identical to what's on the far side. With
    no manifest forcing a write every run, this is a real case, not just theoretical."""
    subprocess.run(["git", *_git_id(), "checkout", "-b", branch], cwd=dest_repo, check=True, capture_output=True)
    subprocess.run(["git", *_git_id(), "add", "-A"], cwd=dest_repo, check=True, capture_output=True)

    nothing_staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=dest_repo).returncode == 0
    if nothing_staged:
        subprocess.run(["git", *_git_id(), "checkout", "-"], cwd=dest_repo, check=True, capture_output=True)
        subprocess.run(["git", *_git_id(), "branch", "-D", branch], cwd=dest_repo, check=True, capture_output=True)
        return False

    subprocess.run(["git", *_git_id(), "commit", "-m", message], cwd=dest_repo, check=True, capture_output=True)
    return True


def reword_commit(dest_repo: Path, message: str) -> None:
    """Rewrites the message of the commit `commit_to_branch` just made, before it's
    ever pushed -- amending a not-yet-pushed local commit is ordinary git, not a
    rewrite of shared history. Used to swap the mechanical placeholder message for
    pr_writer.py's already-generated, already-safe title (see design-history.md's
    v14 note) -- this is NOT a reopening of v7's leak: the replacement text comes
    from the same far-side-only, already-scrubbed context the PR body already uses,
    never from anything production-side."""
    subprocess.run(["git", *_git_id(), "commit", "--amend", "-m", message], cwd=dest_repo, check=True, capture_output=True)


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

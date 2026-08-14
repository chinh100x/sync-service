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
    This is only a *temporary* working name used while committing, needed because
    diff.candidate_diff() (used to build the PR title) requires a real commit on a
    real branch to diff against, before that title exists. cli.py renames it to a
    clean, human-readable, title-derived name (see rename_branch) once the title is
    known, before anything is pushed -- this sha-only name is never itself pushed or
    visible anywhere. Idempotency no longer depends on this name at all -- see
    already_synced/record_synced, which track (mapping, head_sha) via a dedicated ref
    instead, so the final branch name is free to carry no sha at all. See
    design-history.md's v20 note."""
    return f"{namespace}/{head_sha[:7]}"


def _sync_ref(mapping_key: str, head_sha: str) -> str:
    return f"refs/sync-service/{mapping_key}/{head_sha}"


def already_synced(dest_repo: Path, mapping_key: str, head_sha: str) -> bool:
    """Whether this (mapping, head_sha) has already been proposed. Checked via a
    dedicated, non-branch ref recorded by record_synced on a prior successful run --
    not the PR branch's own name, which is now a plain, readable slug with no sha in
    it (see design-history.md's v20 note) and so can no longer double as the
    idempotency key the way the old sha-prefixed name did."""
    ref = _sync_ref(mapping_key, head_sha)
    local = subprocess.run(["git", "show-ref", "--verify", "--quiet", ref], cwd=dest_repo)
    if local.returncode == 0:
        return True

    remote = subprocess.run(["git", "ls-remote", "origin", ref], cwd=dest_repo, capture_output=True, text=True)
    return bool(remote.stdout.strip())


def record_synced(dest_repo: Path, mapping_key: str, head_sha: str, token: str | None = None) -> None:
    """Marks (mapping, head_sha) as synced, for already_synced to find on a later
    run -- call only after a real PR (or dry-run) actually succeeded, never on a
    halt/failure, so a genuinely failed run still gets retried rather than silently
    skipped forever. Best-effort on the remote push (mirrors the branch push in
    open_pr): a real remote is required for this to mean anything past the current
    checkout, but a failure here shouldn't fail the overall run -- worst case, a
    future re-run just doesn't recognize this one and opens a redundant PR, not a
    silent incorrect skip."""
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


def commit_to_branch(dest_repo: Path, branch: str, message: str, author: str | None = None) -> bool:
    """Assumes dest_repo's working tree already has base_branch checked out with the
    scrubbed changes pending (uncommitted). Moves them onto a new branch.

    `author` (a "<name> <email>" string), when given, credits the actual near-side
    committer via git's Author field, distinct from the Committer field (still
    `_git_id()`, the bot identity) -- git itself already keeps these separate, this
    just uses that instead of collapsing both to the bot. See design-history.md's
    v19 note.

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

    commit_cmd = ["git", *_git_id(), "commit", "-m", message]
    if author:
        commit_cmd += ["--author", author]
    subprocess.run(commit_cmd, cwd=dest_repo, check=True, capture_output=True)
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

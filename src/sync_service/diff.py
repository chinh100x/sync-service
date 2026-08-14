"""Trigger: which mappings did base..head touch, and what does the resulting
OSS-side diff/commit metadata look like?"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Mapping


def changed_files(repo_path: str | Path, base: str, head: str) -> list[str]:
    """Files touched between base and head, relative to repo root."""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}..{head}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def commit_author(repo_path: str | Path, sha: str) -> str:
    """`<name> <email>` for the production commit at `sha`, for crediting the
    OSS-side commit's Author field. Reads only name/email, never the message/diff/tree."""
    out = subprocess.run(
        ["git", "show", "-s", "--format=%an <%ae>", sha],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def candidate_diff(dest_repo: str | Path, base_branch: str, branch: str) -> str:
    """Diff entirely within dest_repo's own history -- never reads the production
    repo, so it only shows what scrub.apply() already wrote. Feeds pr_writer.py's
    sanitized_diff."""
    proc = subprocess.run(
        ["git", "diff", f"{base_branch}..{branch}"],
        cwd=dest_repo,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def match(files: list[str], mappings: list[Mapping]) -> dict[str, list[str]]:
    """mapping.key -> touched files under mapping.source, for mappings actually touched."""
    hits: dict[str, list[str]] = {}
    for m in mappings:
        if m.source in (".", "./", ""):
            touched = list(files)  # whole-repo mapping — every changed file is "under" it
        else:
            path = m.source.rstrip("/") + "/"
            touched = [f for f in files if f.startswith(path) or f == path.rstrip("/")]
        if touched:
            hits[m.key] = touched
    return hits

"""Trigger: which mappings did base..head touch, and what does the resulting
far-side diff/commit metadata look like?"""
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
    """`<name> <email>` for the near-side commit at `sha`, for crediting the far-side
    commit's Author field. Reads only name/email, never the message/diff/tree."""
    out = subprocess.run(
        ["git", "show", "-s", "--format=%an <%ae>", sha],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def candidate_diff(far_repo: str | Path, base_branch: str, branch: str) -> str:
    """Diff entirely within far_repo's own history -- never reads near_repo, so it
    only shows what scrub.apply() already wrote. Feeds pr_writer.py's sanitized_diff."""
    proc = subprocess.run(
        ["git", "diff", f"{base_branch}..{branch}"],
        cwd=far_repo,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def match(files: list[str], mappings: list[Mapping], path_attr: str = "source") -> dict[str, list[str]]:
    """mapping.key -> touched files under mapping.<path_attr>, for mappings actually
    touched. path_attr is "source" forward, "dest" reverse -- same logic either way."""
    hits: dict[str, list[str]] = {}
    for m in mappings:
        raw = getattr(m, path_attr)
        if raw in (".", "./", ""):
            touched = list(files)  # whole-repo mapping — every changed file is "under" it
        else:
            path = raw.rstrip("/") + "/"
            touched = [f for f in files if f.startswith(path) or f == path.rstrip("/")]
        if touched:
            hits[m.key] = touched
    return hits

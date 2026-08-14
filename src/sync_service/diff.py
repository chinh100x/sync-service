"""Trigger — design.md §1. Which mappings did base..head touch?"""
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
    """`<name> <email>` for the near-side commit at `sha`, as recorded in that repo's
    own history -- the actual author of this change, for crediting on the far-side
    commit (see design-history.md's v19 note). Reads only name/email metadata, never
    the commit message/diff/tree, so this can't leak anything scrub/secretscan is
    responsible for scrubbing."""
    out = subprocess.run(
        ["git", "show", "-s", "--format=%an <%ae>", sha],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def candidate_diff(far_repo: str | Path, base_branch: str, branch: str) -> str:
    """Unified diff of the OSS candidate actually committed -- `git diff` entirely
    within far_repo's own history (base_branch vs. the just-created sync branch).
    Never reads near_repo/production at all, by construction: this only sees what
    scrub.apply() already wrote to far_repo, after redaction/exclusion. Used to build
    pr_writer.py's PRContext.sanitized_diff -- the one thing an LLM PR writer is
    allowed to see of the actual code change.
    """
    proc = subprocess.run(
        ["git", "diff", f"{base_branch}..{branch}"],
        cwd=far_repo,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def match(files: list[str], mappings: list[Mapping], path_attr: str = "source") -> dict[str, list[str]]:
    """mapping.key -> touched files under mapping.<path_attr>, for mappings actually touched.

    path_attr="source" for the forward trigger (production -> OSS); "dest" for the reverse
    trigger (OSS -> production) — same matching logic, different side of the mapping.
    """
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

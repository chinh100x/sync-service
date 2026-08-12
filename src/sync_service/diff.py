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


def commit_message(repo_path: str | Path, sha: str) -> str:
    """The original commit's own message — subject + body, exactly as the human wrote it."""
    out = subprocess.run(
        ["git", "log", "-1", "--pretty=%B", sha],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def match(files: list[str], mappings: list[Mapping], path_attr: str = "source") -> dict[str, list[str]]:
    """mapping.key -> touched files under mapping.<path_attr>, for mappings actually touched.

    path_attr="source" for the forward trigger (production -> OSS); "dest" for the reverse
    trigger (OSS -> production) — same matching logic, different side of the mapping.
    """
    hits: dict[str, list[str]] = {}
    for m in mappings:
        path = getattr(m, path_attr).rstrip("/") + "/"
        touched = [f for f in files if f.startswith(path) or f == path.rstrip("/")]
        if touched:
            hits[m.key] = touched
    return hits

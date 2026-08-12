"""Scrub list — design.md §2. Exclude list + regex substitution. Mechanical, no AI judgment.

Direction-agnostic: forward (production -> OSS) applies `redact` walking `mapping.source`
into `mapping.dest`; reverse (OSS -> production) applies `hydrate` walking `mapping.dest`
into `mapping.source` — same function, arguments swapped by the caller.
"""
from __future__ import annotations

from pathlib import Path

from .config import RedactRule


def apply(
    repo_root: Path,
    source: str,
    dest: str,
    exclude: list[str],
    transform_rules: list[RedactRule],
) -> dict[str, str]:
    """Copy repo_root/source -> dest with excludes dropped and transform_rules applied.

    Returns {relative_dest_path: file_contents} for every file that survives.
    """
    source_root = repo_root / source
    excluded = {str(Path(p)) for p in exclude}
    desired: dict[str, str] = {}

    if not source_root.exists():
        return desired

    for path in sorted(source_root.rglob("*")):
        if path.is_dir():
            continue
        rel_to_source = path.relative_to(repo_root)
        if str(rel_to_source) in excluded or any(
            str(rel_to_source).startswith(e.rstrip("/") + "/") for e in excluded
        ):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue  # binary files pass through untouched by substitution; skip in this demo

        for rule in transform_rules:
            text = rule.compiled.sub(rule.replace, text)

        rel_to_dest = Path(dest) / path.relative_to(source_root)
        desired[str(rel_to_dest)] = text

    return desired

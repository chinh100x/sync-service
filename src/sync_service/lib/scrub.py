"""Scrub: exclude list + regex substitution. Mechanical, no AI judgment.
Applies `redact`, walking `mapping.source` into `mapping.dest`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import RedactRule

# Always excluded regardless of the mapping's own exclude list -- mechanical
# necessities (repo internals, sync bookkeeping), not a content decision. `.git`
# itself never needs an entry here: `git ls-files` structurally can never return
# anything under it. `.sync-state`/`.sync-service-target` stay as a backstop in
# case either ever ends up committed by accident. Public (not `_`-prefixed):
# patch.py's per-commit replay path applies the same mechanical exclusion to
# whatever a single commit touches, not just a full-tree walk.
ALWAYS_EXCLUDE = {".sync-state", ".sync-service-target"}


def _tracked_files(repo_root: Path, source: str) -> list[str]:
    """Paths git actually tracks under repo_root/source, relative to repo_root.

    Walking `git ls-files` instead of the raw filesystem means this respects
    whatever the source repo's own .gitignore already excludes -- build
    artifacts, caches, editor droppings -- rather than sweeping in anything
    that happens to be sitting in the working tree at sync time. A stale
    .pytest_cache/ directory (real incident: it kept a covenant-specific test
    name around after the source file itself had been genericized) is exactly
    the kind of thing this is meant to keep out."""
    proc = subprocess.run(
        ["git", "ls-files", "--", source],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.splitlines()


def redact_text(text: str, transform_rules: list[RedactRule]) -> tuple[str, list[str]]:
    """The actual mechanical substitution, against a plain string -- no file
    I/O. Returns (redacted_text, categories_triggered). Shared by `apply()`
    (redacts a full-tree snapshot) and patch.py's per-commit replay (redacts
    one file's content read via `git show`, in memory, before it's ever
    written to dest_repo's working tree at all)."""
    categories: set[str] = set()
    for rule in transform_rules:
        before = text
        text = rule.compiled.sub(rule.replace, text)
        if text != before and rule.category:
            categories.add(rule.category)
    return text, sorted(categories)


def apply(
    repo_root: Path,
    source: str,
    dest: str,
    exclude: list[str],
    transform_rules: list[RedactRule],
) -> tuple[dict[str, str], list[str]]:
    """Copy repo_root/source -> dest with excludes dropped and transform_rules applied.

    Returns ({relative_dest_path: file_contents}, categories_triggered) -- the second
    is the deduped set of rule categories that actually replaced something (not
    just every category present in config). Feeds pr_writer.py's scrubbed_categories."""
    excluded = {str(Path(p)) for p in exclude}
    desired: dict[str, str] = {}
    categories: set[str] = set()

    for rel in _tracked_files(repo_root, source):
        rel_to_source = Path(rel)
        if rel_to_source.parts[0] in ALWAYS_EXCLUDE:
            continue
        if str(rel_to_source) in excluded or any(
            str(rel_to_source).startswith(e.rstrip("/") + "/") for e in excluded
        ):
            continue

        path = repo_root / rel_to_source
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue  # binary files pass through untouched by substitution; skip in this demo

        text, fired = redact_text(text, transform_rules)
        categories |= set(fired)

        rel_to_dest = Path(dest) / rel_to_source.relative_to(source if source != "." else "")
        desired[str(rel_to_dest)] = text

    return desired, sorted(categories)

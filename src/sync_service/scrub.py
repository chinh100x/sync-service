"""Scrub list — design.md §2. Exclude list + regex substitution. Mechanical, no AI judgment.

Direction-agnostic: forward (production -> OSS) applies `redact` walking `mapping.source`
into `mapping.dest`; reverse (OSS -> production) applies `hydrate` walking `mapping.dest`
into `mapping.source` — same function, arguments swapped by the caller.
"""
from __future__ import annotations

from pathlib import Path

from .config import RedactRule

# Always excluded, regardless of the mapping's own exclude list — mechanical necessities
# of the tool itself, not a content decision. Relevant now that source/dest default to
# the whole repo: without this, a full-repo mapping would try to copy the other repo's
# .git internals, each side's own sync bookkeeping, and — since actions/checkout won't
# place a checkout outside $GITHUB_WORKSPACE — the counterpart checkout action.yml makes,
# into the other repo's tree.
_ALWAYS_EXCLUDE = {".git", ".sync-state", ".sync-service-counterpart"}


def apply(
    repo_root: Path,
    source: str,
    dest: str,
    exclude: list[str],
    transform_rules: list[RedactRule],
) -> tuple[dict[str, str], list[str]]:
    """Copy repo_root/source -> dest with excludes dropped and transform_rules applied.

    Returns ({relative_dest_path: file_contents}, categories_triggered) -- the second
    element is the sorted, deduped set of transform_rules[i].category for rules that
    actually replaced something in at least one file (not just every category present
    in config). Consumed by pr_writer.py's PRContext.scrubbed_categories -- a category
    label, never the matched text itself.
    """
    source_root = repo_root / source
    excluded = {str(Path(p)) for p in exclude}
    desired: dict[str, str] = {}
    categories: set[str] = set()

    if not source_root.exists():
        return desired, []

    for path in sorted(source_root.rglob("*")):
        if path.is_dir():
            continue
        rel_to_source = path.relative_to(repo_root)
        if rel_to_source.parts[0] in _ALWAYS_EXCLUDE:
            continue
        if str(rel_to_source) in excluded or any(
            str(rel_to_source).startswith(e.rstrip("/") + "/") for e in excluded
        ):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue  # binary files pass through untouched by substitution; skip in this demo

        for rule in transform_rules:
            before = text
            text = rule.compiled.sub(rule.replace, text)
            if text != before and rule.category:
                categories.add(rule.category)

        rel_to_dest = Path(dest) / path.relative_to(source_root)
        desired[str(rel_to_dest)] = text

    return desired, sorted(categories)

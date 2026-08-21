"""Scrub: regex substitution. Mechanical, no AI judgment. Applied by
patch.py's per-commit replay against one file's content at a time, in
memory, before it's ever written into the OSS repo's working tree.
"""

from __future__ import annotations

from .config import RedactRule

# Always excluded regardless of the mapping's own exclude list -- mechanical
# necessities (repo internals, sync bookkeeping), not a content decision.
# Public: patch.py's is_excluded() applies this on top of the mapping's own
# exclude list.
ALWAYS_EXCLUDE = {".sync-state", ".sync-service-target"}


def redact_text(text: str, transform_rules: list[RedactRule]) -> tuple[str, list[str]]:
    """The actual mechanical substitution, against a plain string -- no file
    I/O. Returns (redacted_text, categories_triggered) -- the second is the
    deduped set of rule categories that actually replaced something (not just
    every category present in config). Feeds pr_writer.py's scrubbed_categories."""
    categories: set[str] = set()
    for rule in transform_rules:
        before = text
        text = rule.compiled.sub(rule.replace, text)
        if text != before and rule.category:
            categories.add(rule.category)
    return text, sorted(categories)

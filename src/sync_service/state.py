"""Sync-state manifest — design.md §2/architecture.md §5. A hash comparison over a known
file set, so a re-sync never clobbers a file an outside contributor touched since the
last sync. Any divergence is a hard stop for a human — no auto-merge attempted; see
design.md's v4 note for why the three-way-merge version of this got reverted.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


def manifest_path(dest_root: Path, mapping_key: str) -> Path:
    return dest_root / ".sync-state" / f"{mapping_key}.json"


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def load(dest_root: Path, mapping_key: str) -> dict:
    p = manifest_path(dest_root, mapping_key)
    if not p.exists():
        return {"last_source_sha": None, "files": {}}
    return json.loads(p.read_text())


def write(dest_root: Path, mapping_key: str, source_sha: str, desired: dict[str, str]) -> None:
    p = manifest_path(dest_root, mapping_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "last_source_sha": source_sha,
        "files": {rel: _hash(text) for rel, text in desired.items()},
    }
    p.write_text(json.dumps(manifest, indent=2) + "\n")


@dataclass
class PathResult:
    status: str  # "clean" | "new" | "conflict"
    write_content: str | None  # what to write; None for "conflict"


def classify(dest_root: Path, manifest: dict, desired: dict[str, str]) -> dict[str, PathResult]:
    """For every path this run wants to write: unchanged, brand-new, or diverged (=conflict)."""
    known = manifest.get("files", {})
    results: dict[str, PathResult] = {}

    for rel_path, new_text in desired.items():
        current_file = dest_root / rel_path
        if not current_file.exists():
            results[rel_path] = PathResult("new", new_text)
            continue

        current_hash = _hash(current_file.read_text())
        if rel_path in known and current_hash == known[rel_path]:
            results[rel_path] = PathResult("clean", new_text)
        else:
            # Either the far side edited this since our last sync, or it exists with no
            # recorded baseline at all (unknown provenance). Either way: don't overwrite.
            results[rel_path] = PathResult("conflict", None)

    return results

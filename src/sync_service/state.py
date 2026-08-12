"""Sync-state manifest — v2. Not just a hash comparison anymore: the manifest stores the
actual last-synced content (as content-addressed blobs) so a divergence on the far side
can attempt a real three-way merge (base = last sync, ours = far side now, theirs = the
new content this run wants to write) instead of an unconditional hard stop.

A merge that resolves cleanly writes the merged result — no human needed. A merge that
genuinely conflicts (both sides touched the same lines) still halts for a human, per
design.md's original policy: auto-resolving a real conflict is out of scope, only
*detecting* whether one exists changed.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


def manifest_path(dest_root: Path, mapping_key: str) -> Path:
    return dest_root / ".sync-state" / f"{mapping_key}.json"


def _blob_dir(dest_root: Path, mapping_key: str) -> Path:
    return dest_root / ".sync-state" / mapping_key / "blobs"


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def load(dest_root: Path, mapping_key: str) -> dict:
    p = manifest_path(dest_root, mapping_key)
    if not p.exists():
        return {"last_source_sha": None, "files": {}}
    return json.loads(p.read_text())


def _load_blob(dest_root: Path, mapping_key: str, content_hash: str) -> str | None:
    digest = content_hash.split(":", 1)[1]
    blob = _blob_dir(dest_root, mapping_key) / f"{digest}.blob"
    return blob.read_text() if blob.exists() else None


def write(dest_root: Path, mapping_key: str, source_sha: str, desired: dict[str, str]) -> None:
    p = manifest_path(dest_root, mapping_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    blob_dir = _blob_dir(dest_root, mapping_key)
    blob_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {}
    for rel, text in desired.items():
        h = _hash(text)
        files[rel] = h
        (blob_dir / f"{h.split(':', 1)[1]}.blob").write_text(text)

    manifest = {"last_source_sha": source_sha, "files": files}
    p.write_text(json.dumps(manifest, indent=2) + "\n")


def merge3(base_text: str, ours_text: str, theirs_text: str) -> tuple[str, bool]:
    """Three-way merge via `git merge-file`. Returns (merged_text, has_conflict)."""
    with tempfile.TemporaryDirectory() as td:
        ours = Path(td) / "ours"
        base = Path(td) / "base"
        theirs = Path(td) / "theirs"
        ours.write_text(ours_text)
        base.write_text(base_text)
        theirs.write_text(theirs_text)
        proc = subprocess.run(
            ["git", "merge-file", "-p", "--diff3", str(ours), str(base), str(theirs)],
            capture_output=True,
            text=True,
        )
        return proc.stdout, proc.returncode != 0


@dataclass
class PathResult:
    status: str  # "clean" | "new" | "merged" | "conflict"
    write_content: str | None  # what forward/reverse should write; None only for "conflict"
    far_side_text: str | None = None  # current content on the far side, if it had diverged
    base_text: str | None = None  # last-synced content, if we had one to merge from


def classify(dest_root: Path, manifest: dict, desired: dict[str, str], mapping_key: str) -> dict[str, PathResult]:
    """For every path this run wants to write, decide: unchanged, brand-new, cleanly
    mergeable with whatever the far side did since last sync, or a real conflict."""
    known = manifest.get("files", {})
    results: dict[str, PathResult] = {}

    for rel_path, new_text in desired.items():
        current_file = dest_root / rel_path
        if not current_file.exists():
            results[rel_path] = PathResult("new", new_text)
            continue

        current_text = current_file.read_text()
        current_hash = _hash(current_text)

        if rel_path in known and current_hash == known[rel_path]:
            results[rel_path] = PathResult("clean", new_text)
            continue

        base_text = _load_blob(dest_root, mapping_key, known[rel_path]) if rel_path in known else None
        if base_text is None:
            # Exists on the far side, but we have no recorded baseline for it — unknown
            # provenance, nothing safe to three-way-merge against. Conservative: conflict.
            results[rel_path] = PathResult("conflict", None, far_side_text=current_text)
            continue

        merged, conflicted = merge3(base_text, current_text, new_text)
        if conflicted:
            results[rel_path] = PathResult(
                "conflict", None, far_side_text=current_text, base_text=base_text
            )
        else:
            results[rel_path] = PathResult(
                "merged", merged, far_side_text=current_text, base_text=base_text
            )

    return results

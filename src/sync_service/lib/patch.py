"""Per-commit patch replay: the mechanics for turning one source commit into one
plain-text, path-remapped, exclude-filtered diff that can be staged onto the dest
repo's working tree without ever fetching the source commit *object* into dest's
own git database.

Why not `git cherry-pick`: it needs the source commit to already exist in dest's
object store, which means fetching prod's raw, unscrubbed blobs into oss's git
database before scrub.py ever gets a chance to redact them -- backwards from the
whole point of the redact step. A unified diff is just text; applying it to the
working tree alone (never `git apply --index`) means nothing touches dest's
object database until sync.py explicitly stages the *already-redacted* result via
`git add`/`git commit` -- staging is what hashes content into a real blob object,
not applying a patch to plain files on disk, so redaction has to happen before
that staging step, not merely before the commit. It also gets create/update/delete
parity for free -- a deleted file is `--- a/path` / `+++ /dev/null` in the patch
itself, which `git apply` deletes on application, no separate reconciliation step
needed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")


class PatchApplyFailed(Exception):
    """A commit's patch didn't apply cleanly onto dest's current working tree --
    the replay equivalent of a cherry-pick conflict. Callers must halt, not
    attempt any kind of automatic resolution."""


def commits_between(repo_path: str | Path, base: str, head: str, source: str) -> list[str]:
    """Non-merge commits touching `source`, oldest first -- the replay order."""
    pathspec = [] if source in (".", "./", "") else ["--", source]
    out = subprocess.run(
        ["git", "log", "--reverse", "--no-merges", "--format=%H", f"{base}..{head}", *pathspec],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def merge_commits_between(repo_path: str | Path, base: str, head: str, source: str) -> list[str]:
    """Merge commits touching `source` -- checked only to give a specific halt
    reason when one is the sole thing in range (commits_between excludes these
    entirely; a merge can't be replayed as a single linear patch).

    `--full-history`: git log's default path simplification hides a merge
    entirely whenever only one parent actually touched `source` (the common
    case for a routine, non-conflicting merge) -- exactly the case this needs
    to still see, so it isn't silently swallowed and misreported as "empty"."""
    pathspec = [] if source in (".", "./", "") else ["--", source]
    out = subprocess.run(
        [
            "git",
            "log",
            "--reverse",
            "--merges",
            "--full-history",
            "--format=%H",
            f"{base}..{head}",
            *pathspec,
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def extract_patch(repo_path: str | Path, sha: str, source: str) -> str:
    """This one commit's change, scoped to `source`, as a plain-text unified
    diff -- nothing here touches any git object database."""
    pathspec = [] if source in (".", "./", "") else ["--", source]
    proc = subprocess.run(
        ["git", "diff", "--binary", f"{sha}^..{sha}", *pathspec],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def has_binary_content(patch_text: str) -> bool:
    """Binary hunks can't be regex-redacted -- treated as an automatic halt by
    the caller, the same "bias toward blocking when uncertain" posture
    safety_review.py already follows."""
    return "GIT binary patch" in patch_text or "Binary files " in patch_text


def _split_blocks(patch_text: str) -> list[str]:
    if not patch_text:
        return []
    indices = [m.start() for m in re.finditer(r"^diff --git ", patch_text, re.MULTILINE)]
    if not indices:
        return []
    indices.append(len(patch_text))
    return [patch_text[indices[i] : indices[i + 1]] for i in range(len(indices) - 1)]


def _paths_from_block(block: str) -> tuple[str | None, str | None]:
    """(old_path, new_path), relative, or None for /dev/null (create/delete).
    The `diff --git a/X b/Y` header is the fallback (needed for a binary block,
    which has no --- / +++ lines); real --- / +++ / rename lines, read after
    it, override with the authoritative paths whenever present."""
    old_path = new_path = None
    lines = block.splitlines()
    if lines:
        m = _FILE_HEADER_RE.match(lines[0])
        if m:
            old_path, new_path = m.group(1), m.group(2)
    for line in lines[1:]:
        if line.startswith("--- "):
            old_path = None if line[4:] == "/dev/null" else line[6:]
        elif line.startswith("+++ "):
            new_path = None if line[4:] == "/dev/null" else line[6:]
        elif line.startswith("rename from "):
            old_path = line[len("rename from ") :]
        elif line.startswith("rename to "):
            new_path = line[len("rename to ") :]
    return old_path, new_path


def _is_excluded(rel_path: str, excluded: set[str]) -> bool:
    return rel_path in excluded or any(rel_path.startswith(e.rstrip("/") + "/") for e in excluded)


def filter_excluded(patch_text: str, exclude: list[str]) -> str:
    """Drops whole per-file blocks under any of `exclude` -- filtering a
    patch's hunks, not a dict's keys, but the same exclusion rule scrub.py
    already applies to a full-tree snapshot. `exclude` entries are repo-root
    relative (matching scrub.py's own convention -- `git ls-files` there
    already returns repo-root-relative paths despite the `rel_to_source`
    naming), the same as the old/new paths a diff's own headers carry --
    no relativizing to mapping.source needed here."""
    if not exclude:
        return patch_text
    excluded = {str(Path(e)) for e in exclude}
    kept = []
    for block in _split_blocks(patch_text):
        old_path, new_path = _paths_from_block(block)
        rel_paths = [p for p in (old_path, new_path) if p is not None]
        if any(_is_excluded(p, excluded) for p in rel_paths):
            continue
        kept.append(block)
    return "".join(kept)


def _remap(path: str, source: str, dest: str) -> str:
    rel = Path(path)
    if source not in (".", "./", ""):
        rel = rel.relative_to(source)
    if dest not in (".", "./", ""):
        rel = Path(dest) / rel
    return str(rel)


def _remap_block(block: str, source: str, dest: str) -> str:
    out_lines = []
    for line in block.splitlines(keepends=True):
        stripped = line[:-1] if line.endswith("\n") else line
        if stripped.startswith("diff --git "):
            m = _FILE_HEADER_RE.match(stripped)
            if m:
                old, new = m.group(1), m.group(2)
                line = f"diff --git a/{_remap(old, source, dest)} b/{_remap(new, source, dest)}\n"
        elif stripped.startswith("--- a/"):
            line = f"--- a/{_remap(stripped[6:], source, dest)}\n"
        elif stripped.startswith("+++ b/"):
            line = f"+++ b/{_remap(stripped[6:], source, dest)}\n"
        elif stripped.startswith("rename from "):
            line = f"rename from {_remap(stripped[len('rename from '):], source, dest)}\n"
        elif stripped.startswith("rename to "):
            line = f"rename to {_remap(stripped[len('rename to '):], source, dest)}\n"
        elif stripped.startswith("copy from "):
            line = f"copy from {_remap(stripped[len('copy from '):], source, dest)}\n"
        elif stripped.startswith("copy to "):
            line = f"copy to {_remap(stripped[len('copy to '):], source, dest)}\n"
        out_lines.append(line)
    return "".join(out_lines)


def remap_paths(patch_text: str, source: str, dest: str) -> str:
    """Rewrites a/ b/ (and rename/copy) headers from mapping.source to
    mapping.dest -- the same relocation scrub.apply() already does for a full
    snapshot, applied to a patch's headers instead of a dict's keys. `/dev/null`
    lines (create/delete markers) are left untouched -- there's nothing to
    remap for a side that doesn't exist."""
    if source in (".", "./", "") and dest in (".", "./", ""):
        return patch_text
    return "".join(_remap_block(b, source, dest) for b in _split_blocks(patch_text))


def touched_dest_paths(remapped_patch_text: str) -> list[str]:
    """Dest-relative paths this commit leaves behind (i.e. still exist after
    the patch -- a deleted file has no `new_path` and nothing left to read).
    Feeds the redact + secretscan + safety_review steps, which only ever need
    to look at content that's actually still there."""
    paths = []
    for block in _split_blocks(remapped_patch_text):
        _, new_path = _paths_from_block(block)
        if new_path is not None:
            paths.append(new_path)
    return paths


def apply_to_working_tree(dest_repo: str | Path, patch_text: str) -> None:
    """Applies a remapped patch to dest_repo's *working tree only* -- no
    `--index`. Deliberately not staged: `git add`/`git apply --index` hashes
    and writes a real blob object into dest's object database immediately on
    staging, not at commit time, which would mean the raw, pre-redaction
    content briefly becomes a real (if unreferenced) git object before
    scrub.redact_paths ever gets a chance to touch it -- exactly the thing
    this whole module exists to avoid. Redaction must happen against the
    working-tree file *before* anything stages it; staging only happens later,
    in publish.commit_all's own `git add -A`, once the content on disk is
    already the redacted version.

    Raises PatchApplyFailed on anything that doesn't apply cleanly; git apply
    is all-or-nothing per invocation, so a failure here leaves the working
    tree untouched."""
    if not patch_text.strip():
        return
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn"],
        cwd=dest_repo,
        input=patch_text,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise PatchApplyFailed(proc.stderr.strip())

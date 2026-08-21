"""Per-commit patch replay: for each source commit, determine which files a
mapping actually cares about changed, read each one's content directly at that
commit, and hand back what dest_repo should look like for it -- so sync.py can
scrub it in memory and write/delete it, then commit with that source commit's
own real message.

An earlier version of this module parsed git's own unified-diff text (headers,
hunks, rename/copy markers) and applied it with `git apply`. Deliberately moved
away from that: git's patch-text format has real edge cases a hand-rolled
parser gets wrong -- quoted/spaced paths, copies, submodules, binary markers --
and git already has robust, well-tested primitives for every piece this
actually needs: `git diff --name-only` for "what changed", `git ls-tree` for
"does it still exist, and is it a submodule", `git show <sha>:<path>` for
"what's its content now." None of that requires understanding diff-text
format at all, and it closes a gap the old version still had: reading content
directly means the raw value is only ever a Python string in this process's
own memory, scrubbed before sync.py ever writes anything into dest_repo's
working tree -- unlike applying a patch first and redacting a moment later,
there's no window where unredacted content sits on disk at all.

`--no-renames` in `changed_paths` is deliberate, not an oversight: without it,
a rename can appear as a single ambiguous status entry. With it, a rename
always decomposes into an independent delete (old path) + write (new path) --
which costs nothing observable later. git never actually stores "this was a
rename" in a commit; it always re-infers renames at diff-*display*-time by
comparing trees for content similarity, so a human looking at dest_repo's own
history afterward sees the same rename either way, regardless of how this
module represented it internally.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .scrub import ALWAYS_EXCLUDE


class SubmoduleNotSupported(Exception):
    """A touched path is a submodule (gitlink), not a regular file -- there's
    no file content to read or scrub. Callers must halt, not guess at how to
    represent it."""


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
    """Merge commits touching `source` -- checked unconditionally, before
    commits_between even matters, and any result halts the whole batch.
    commits_between excludes merges entirely (a merge can't be replayed as a
    single linear step); a merge's own unique content -- e.g. hand-resolved
    conflict content that exists in neither parent individually -- is never
    captured by replaying its parents alone, confirmed empirically (two
    branches independently setting the same line, a human resolving the
    conflict to a third value; replaying just the two parent commits lands on
    neither value). This applies whether or not non-merge commits also exist
    in the same range -- silently replaying just those and ignoring the merge
    would leave the OSS side with a *wrong* final state, not just an
    incomplete one, so this can't be limited to "only when nothing else is in
    range."

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


def changed_paths(repo_path: str | Path, sha: str, source: str) -> list[str]:
    """Repo-root-relative paths that differ between sha^ and sha, scoped to
    `source`. `-z` (NUL-separated) means quoted/spaced filenames come back as
    exact bytes, never text needing unescaping -- no quoting ambiguity at all,
    unlike parsing a diff header line."""
    pathspec = [] if source in (".", "./", "") else ["--", source]
    proc = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", "-z", f"{sha}^..{sha}", *pathspec],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    return [p.decode() for p in proc.stdout.split(b"\0") if p]


def is_excluded(rel_path: str, exclude: list[str]) -> bool:
    """Same exclusion rule scrub.py's `apply()` already applies to a full-tree
    walk -- the mechanical ALWAYS_EXCLUDE set plus the mapping's own list,
    both checked against the repo-root-relative path."""
    parts = Path(rel_path).parts
    if parts and parts[0] in ALWAYS_EXCLUDE:
        return True
    excluded = {str(Path(e)) for e in exclude}
    return rel_path in excluded or any(rel_path.startswith(e.rstrip("/") + "/") for e in excluded)


def _remap(rel_path: str, source: str, dest: str) -> str:
    rel = Path(rel_path)
    if source not in (".", "./", ""):
        rel = rel.relative_to(source)
    if dest not in (".", "./", ""):
        rel = Path(dest) / rel
    return str(rel)


def _entry_mode(repo_path: str | Path, sha: str, rel_path: str) -> str | None:
    """Git's raw tree-entry mode (e.g. "100644", "120000", "160000") for
    rel_path at sha, or None if it doesn't exist there at all (deleted by this
    commit)."""
    proc = subprocess.run(
        ["git", "ls-tree", "-z", sha, "--", rel_path],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    if not proc.stdout:
        return None
    return proc.stdout.split()[0]


@dataclass
class ResolvedChange:
    """What one changed path resolves to, at `sha`, remapped into dest space.

    `kind`:
    - "write": still exists and is readable text -- `content` is its raw
      (not-yet-redacted) value at this commit.
    - "write_binary": still exists but can't be decoded as text -- `raw` is
      its raw bytes. Propagates as-is: there's no mechanical way to redact
      binary content (regex substitution doesn't apply to bytes), and no way
      to run safety_review's semantic review on it either (an LLM has
      nothing meaningful to judge in raw bytes). secretscan still runs
      against it (see sync.py) -- a credential-shaped ASCII string embedded
      in an otherwise-binary file is still catchable by regex even without
      valid UTF-8. Callers must surface this explicitly (never silently) --
      it's a real, deliberate reduction in gate coverage for this content.
    - "delete": no longer exists at this commit."""

    dest_path: str
    kind: str
    content: str | None = None
    raw: bytes | None = None


def resolve_change(
    repo_path: str | Path, sha: str, source: str, dest: str, rel_path: str
) -> ResolvedChange:
    """Resolves one changed path to what dest_repo should do about it.
    Raises SubmoduleNotSupported for a gitlink -- there's no content to read,
    and silently skipping could ship a broken/missing submodule reference
    with no warning."""
    dest_path = _remap(rel_path, source, dest)
    mode = _entry_mode(repo_path, sha, rel_path)
    if mode is None:
        return ResolvedChange(dest_path=dest_path, kind="delete")
    if mode == "160000":
        raise SubmoduleNotSupported(rel_path)

    proc = subprocess.run(
        ["git", "show", f"{sha}:{rel_path}"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    try:
        content = proc.stdout.decode()
    except UnicodeDecodeError:
        return ResolvedChange(dest_path=dest_path, kind="write_binary", raw=proc.stdout)
    return ResolvedChange(dest_path=dest_path, kind="write", content=content)

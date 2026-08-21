import subprocess

from sync_service.lib import patch

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com"]


def _git(repo, *args):
    return subprocess.run(
        ["git", *GIT_ID, *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _rev(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _rev(repo)


def _write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _init_repo(repo):
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")


def test_commits_between_lists_non_merge_commits_oldest_first(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", "1\n")
    base = _commit(repo, "initial")
    _write(repo, "src/a.py", "2\n")
    first = _commit(repo, "first")
    _write(repo, "src/a.py", "3\n")
    second = _commit(repo, "second")

    commits = patch.commits_between(repo, base, second, "src")

    assert commits == [first, second]


def test_commits_between_scoped_to_source_ignores_unrelated_commits(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", "1\n")
    _write(repo, "docs/readme.md", "x\n")
    base = _commit(repo, "initial")
    _write(repo, "docs/readme.md", "y\n")  # outside `src` -- must not show up
    _commit(repo, "docs only")
    _write(repo, "src/a.py", "2\n")
    relevant = _commit(repo, "touches src")

    commits = patch.commits_between(repo, base, relevant, "src")

    assert commits == [relevant]


def test_merge_commits_between_finds_a_merge_that_touches_source(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", "1\n")
    base = _commit(repo, "initial")

    _git(repo, "checkout", "-b", "side")
    _write(repo, "src/a.py", "2\n")
    _commit(repo, "side change")
    _git(repo, "checkout", "main")
    _write(repo, "docs/readme.md", "x\n")  # unrelated to `src` -- the only main-line commit
    _commit(repo, "main change")
    _git(repo, "merge", "--no-ff", "-m", "merge side", "side")
    head = _rev(repo)

    assert len(patch.merge_commits_between(repo, base, head, "src")) == 1


def test_changed_paths_is_scoped_to_source_only(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", "1\n")
    _write(repo, "docs/readme.md", "x\n")
    _commit(repo, "initial")
    _write(repo, "src/a.py", "2\n")
    _write(repo, "docs/readme.md", "y\n")
    head = _commit(repo, "touches both")

    paths = patch.changed_paths(repo, head, "src")

    assert paths == ["src/a.py"]


def test_changed_paths_lists_a_deleted_file(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", "1\n")
    _commit(repo, "initial")
    (repo / "src" / "a.py").unlink()
    head = _commit(repo, "delete a.py")

    assert patch.changed_paths(repo, head, "src") == ["src/a.py"]


def test_changed_paths_decomposes_a_rename_into_old_and_new(tmp_path):
    # --no-renames is deliberate: a rename must show as two independent paths,
    # never a single ambiguous status entry -- see the module docstring.
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/old.py", "identical content\n")
    _commit(repo, "initial")
    (repo / "src" / "old.py").rename(repo / "src" / "new.py")
    head = _commit(repo, "rename old.py to new.py")

    assert set(patch.changed_paths(repo, head, "src")) == {"src/old.py", "src/new.py"}


def test_changed_paths_handles_a_path_with_spaces(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "README.md", "x\n")
    _commit(repo, "initial")
    _write(repo, "src/file with spaces.py", "1\n")
    head = _commit(repo, "add a file with spaces in its name")

    assert patch.changed_paths(repo, head, "src") == ["src/file with spaces.py"]


def test_is_excluded_checks_the_mechanical_set_and_the_mapping_list():
    assert patch.is_excluded(".sync-state/portmon.json", [])
    assert patch.is_excluded(".sync-service-target/README.md", [])
    assert patch.is_excluded("src/portmon/internal.py", ["src/portmon/internal.py"])
    assert not patch.is_excluded("src/portmon/covenant.py", ["src/portmon/internal.py"])


def test_resolve_change_write_reads_current_content_and_remaps_the_path(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/portmon/a.py", "1\n")
    _commit(repo, "initial")
    _write(repo, "src/portmon/a.py", "2\n")
    head = _commit(repo, "change")

    change = patch.resolve_change(repo, head, "src/portmon", "plugin", "src/portmon/a.py")

    assert change.kind == "write"
    assert change.dest_path == "plugin/a.py"
    assert change.content == "2\n"


def test_resolve_change_delete_when_path_no_longer_exists_at_sha(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/portmon/a.py", "1\n")
    _commit(repo, "initial")
    (repo / "src" / "portmon" / "a.py").unlink()
    head = _commit(repo, "delete a.py")

    change = patch.resolve_change(repo, head, "src/portmon", "plugin", "src/portmon/a.py")

    assert change.kind == "delete"
    assert change.dest_path == "plugin/a.py"
    assert change.content is None


def test_resolve_change_write_binary_for_binary_content(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "readme.txt", "hello\n")
    _commit(repo, "initial")
    raw = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    (repo / "img.png").write_bytes(raw)
    head = _commit(repo, "add binary")

    change = patch.resolve_change(repo, head, ".", ".", "img.png")

    assert change.kind == "write_binary"
    assert change.raw == raw
    assert change.content is None


def test_resolve_change_raises_for_a_submodule(tmp_path):
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    _init_repo(repo)
    _write(repo, "README.md", "x\n")
    _commit(repo, "initial")
    _init_repo(other)
    _write(other, "README.md", "vendored dependency\n")
    _commit(other, "initial")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(other), "vendor/other")
    head = _commit(repo, "add submodule")

    try:
        patch.resolve_change(repo, head, ".", ".", "vendor/other")
        raise AssertionError("expected SubmoduleNotSupported")
    except patch.SubmoduleNotSupported:
        pass


def test_resolve_change_is_a_noop_remap_for_whole_repo_mapping(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "README.md", "x\n")
    _commit(repo, "initial")
    _write(repo, "a.py", "1\n")
    head = _commit(repo, "add a.py")

    change = patch.resolve_change(repo, head, ".", ".", "a.py")

    assert change.dest_path == "a.py"


def test_resolve_change_never_creates_a_git_object_for_the_raw_content(tmp_path):
    # The bug the earlier apply-based version had: `git apply --index` hashes
    # and writes a real blob object at *staging* time, before redaction ever
    # runs -- so raw content could briefly exist as a real git object. Reading
    # content via resolve_change and redacting it in Python, before anything
    # is ever written into dest_repo at all, means the raw value never touches
    # dest_repo's object database in the first place.
    source = tmp_path / "source"
    _init_repo(source)
    _write(source, "src/a.py", "safe\n")
    _commit(source, "initial")
    _write(source, "src/a.py", "RAW_SECRET_MARKER\n")
    head = _commit(source, "change")

    dest = tmp_path / "dest"
    _init_repo(dest)
    _write(dest, "plugin/a.py", "safe\n")
    _commit(dest, "initial")

    change = patch.resolve_change(source, head, "src", "plugin", "src/a.py")
    assert change.content == "RAW_SECRET_MARKER\n"  # read correctly in memory...

    # ...but never written to dest_repo's working tree or object database at all.
    all_blobs = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"],
        cwd=dest,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in all_blobs.splitlines():
        sha, kind = line.split()
        if kind != "blob":
            continue
        content = subprocess.run(
            ["git", "cat-file", "-p", sha], cwd=dest, capture_output=True, text=True, check=True
        ).stdout
        assert "RAW_SECRET_MARKER" not in content

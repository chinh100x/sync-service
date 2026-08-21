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


def test_extract_patch_is_scoped_to_source_only(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", "1\n")
    _write(repo, "docs/readme.md", "x\n")
    _commit(repo, "initial")
    _write(repo, "src/a.py", "2\n")
    _write(repo, "docs/readme.md", "y\n")
    head = _commit(repo, "touches both")

    text = patch.extract_patch(repo, head, "src")

    assert "a.py" in text
    assert "readme.md" not in text


def test_extract_patch_represents_a_deletion(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", "1\n")
    _commit(repo, "initial")
    (repo / "src" / "a.py").unlink()
    head = _commit(repo, "delete a.py")

    text = patch.extract_patch(repo, head, "src")

    assert "deleted file mode" in text
    assert "+++ /dev/null" in text


def test_has_binary_content_detects_a_binary_hunk(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "readme.txt", "hello\n")
    _commit(repo, "initial")
    (repo / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
    head = _commit(repo, "add binary")

    text = patch.extract_patch(repo, head, ".")

    assert patch.has_binary_content(text)


def test_has_binary_content_is_false_for_plain_text_diff(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "a.py", "1\n")
    _commit(repo, "initial")
    _write(repo, "a.py", "2\n")
    head = _commit(repo, "change")

    text = patch.extract_patch(repo, head, ".")

    assert not patch.has_binary_content(text)


def test_remap_paths_relocates_source_subdir_into_dest_subdir(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/portmon/a.py", "1\n")
    _commit(repo, "initial")
    _write(repo, "src/portmon/a.py", "2\n")
    head = _commit(repo, "change")

    raw = patch.extract_patch(repo, head, "src/portmon")
    remapped = patch.remap_paths(raw, "src/portmon", "plugin")

    assert "a/plugin/a.py" in remapped
    assert "b/plugin/a.py" in remapped
    assert "src/portmon" not in remapped


def test_remap_paths_is_a_noop_for_whole_repo_mapping(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "a.py", "1\n")
    _commit(repo, "initial")
    _write(repo, "a.py", "2\n")
    head = _commit(repo, "change")

    raw = patch.extract_patch(repo, head, ".")

    assert patch.remap_paths(raw, ".", ".") == raw


def test_remap_paths_leaves_dev_null_alone_for_a_new_file(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/portmon/a.py", "1\n")
    _commit(repo, "initial")
    _write(repo, "src/portmon/b.py", "2\n")
    head = _commit(repo, "add b.py")

    raw = patch.extract_patch(repo, head, "src/portmon")
    remapped = patch.remap_paths(raw, "src/portmon", "plugin")

    assert "--- /dev/null" in remapped
    assert "+++ b/plugin/b.py" in remapped


def test_filter_excluded_drops_the_whole_block_for_an_excluded_file(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", "1\n")
    _write(repo, "src/secret.py", "1\n")
    _commit(repo, "initial")
    _write(repo, "src/a.py", "2\n")
    _write(repo, "src/secret.py", "2\n")
    head = _commit(repo, "change both")

    raw = patch.extract_patch(repo, head, "src")
    filtered = patch.filter_excluded(raw, ["src/secret.py"])

    assert "a.py" in filtered
    assert "secret.py" not in filtered


def test_touched_dest_paths_excludes_deleted_files(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", "1\n")
    _write(repo, "src/b.py", "1\n")
    _commit(repo, "initial")
    (repo / "src" / "b.py").unlink()
    _write(repo, "src/a.py", "2\n")
    head = _commit(repo, "modify a, delete b")

    raw = patch.extract_patch(repo, head, "src")
    remapped = patch.remap_paths(raw, "src", "plugin")

    assert patch.touched_dest_paths(remapped) == ["plugin/a.py"]


def test_apply_to_working_tree_creates_updates_and_deletes_in_one_shot(tmp_path):
    source = tmp_path / "source"
    _init_repo(source)
    _write(source, "src/a.py", "1\n")
    _write(source, "src/b.py", "1\n")
    _commit(source, "initial")
    _write(source, "src/a.py", "2\n")  # modify
    # Deliberately distinct content from b.py -- identical content here would make
    # git's own diff rename-detection treat this as "b.py renamed to c.py" instead
    # of an independent create + delete, which isn't what this test is exercising.
    _write(source, "src/c.py", "brand new content\n")  # create
    (source / "src" / "b.py").unlink()  # delete
    head = _commit(source, "modify, create, delete")

    dest = tmp_path / "dest"
    _init_repo(dest)
    _write(dest, "plugin/a.py", "1\n")
    _write(dest, "plugin/b.py", "1\n")
    _commit(dest, "initial")

    raw = patch.extract_patch(source, head, "src")
    remapped = patch.remap_paths(raw, "src", "plugin")
    patch.apply_to_working_tree(dest, remapped)

    assert (dest / "plugin" / "a.py").read_text() == "2\n"
    assert (dest / "plugin" / "c.py").read_text() == "brand new content\n"
    assert not (dest / "plugin" / "b.py").exists()
    # Deliberately NOT staged -- see apply_to_working_tree's docstring for why
    # (staging is what hashes content into a real git object; that must only
    # happen after redaction, which runs against these working-tree files).
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-status"],
        cwd=dest,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert staged == ""
    unstaged = subprocess.run(
        ["git", "diff", "--name-status"], cwd=dest, capture_output=True, text=True, check=True
    ).stdout
    assert "D\tplugin/b.py" in unstaged
    assert "M\tplugin/a.py" in unstaged


def test_apply_to_working_tree_never_creates_a_git_object_for_the_raw_content(tmp_path):
    # The actual bug this guards against: `git apply --index` hashes and writes a
    # real blob object immediately on staging, before redaction ever runs against
    # it -- so the raw, pre-redaction content would briefly exist as a real (if
    # unreferenced) git object. Applying to the working tree only means nothing
    # gets hashed into an object until commit_all's own `git add` runs, by which
    # point redaction has already rewritten the file on disk.
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

    raw = patch.extract_patch(source, head, "src")
    remapped = patch.remap_paths(raw, "src", "plugin")
    patch.apply_to_working_tree(dest, remapped)

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


def test_apply_to_working_tree_raises_on_a_patch_that_does_not_apply_cleanly(tmp_path):
    source = tmp_path / "source"
    _init_repo(source)
    _write(source, "src/a.py", "1\n")
    _commit(source, "initial")
    _write(source, "src/a.py", "2\n")
    head = _commit(source, "change")

    dest = tmp_path / "dest"
    _init_repo(dest)
    # dest's content diverges from what the patch expects to modify.
    _write(dest, "plugin/a.py", "something totally different\n")
    _commit(dest, "initial")

    raw = patch.extract_patch(source, head, "src")
    remapped = patch.remap_paths(raw, "src", "plugin")

    try:
        patch.apply_to_working_tree(dest, remapped)
        raise AssertionError("expected PatchApplyFailed")
    except patch.PatchApplyFailed:
        pass

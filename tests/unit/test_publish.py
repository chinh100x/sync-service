import subprocess

from sync_service import publish

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com"]


def _git(repo, *args):
    subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo):
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "file.txt").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")


def test_commit_to_branch_commits_a_real_change(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("changed\n")

    committed = publish.commit_to_branch(repo, "sync/x/abc123", "a real change")

    assert committed is True
    assert publish.branch_exists(repo, "sync/x/abc123")


def test_commit_to_branch_no_op_when_content_is_identical(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    # no file changes made — propagated content is byte-identical to what's already there

    committed = publish.commit_to_branch(repo, "sync/x/abc123", "nothing actually changed")

    assert committed is False
    assert not publish.branch_exists(repo, "sync/x/abc123")


def _is_ancestor(repo, maybe_ancestor, branch):
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", maybe_ancestor, branch],
        cwd=repo, capture_output=True,
    )
    return proc.returncode == 0


def test_checkout_base_prevents_one_mapping_stacking_on_another(tmp_path):
    # Simulates processing two mappings against the same far_repo in one run, the
    # way cli.py's main() loop does. Without checkout_base() between them, the
    # second mapping's branch would have the first's commit as an ancestor.
    repo = tmp_path / "repo"
    _init_repo(repo)

    (repo / "a.txt").write_text("a\n")
    publish.commit_to_branch(repo, "sync/a/111111111111", "mapping a")

    publish.checkout_base(repo, "main")
    (repo / "b.txt").write_text("b\n")
    publish.commit_to_branch(repo, "sync/b/222222222222", "mapping b")

    assert not _is_ancestor(repo, "sync/a/111111111111", "sync/b/222222222222")


def test_branch_exists_checks_the_remote_too(tmp_path):
    # A fresh Action-runner checkout only knows the branch it was checked out onto
    # (e.g. main) -- a branch a previous run pushed exists only on the remote until
    # explicitly fetched. Without this, idempotency silently stops working outside
    # a long-lived local clone.
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    pusher = tmp_path / "pusher"
    _init_repo(pusher)
    _git(pusher, "remote", "add", "origin", str(bare))
    _git(pusher, "push", "-q", "-u", "origin", "main")
    _git(pusher, "checkout", "-b", "sync/x/aaaaaaaaaaaa")
    _git(pusher, "push", "-q", "-u", "origin", "sync/x/aaaaaaaaaaaa")

    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(bare), str(fresh)], check=True)

    assert publish.branch_exists(fresh, "sync/x/aaaaaaaaaaaa") is True  # remote-only, not local
    assert publish.branch_exists(fresh, "sync/x/does-not-exist") is False


def test_commit_to_branch_credits_the_given_author_but_keeps_the_bot_as_committer(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("changed\n")

    publish.commit_to_branch(
        repo, "sync/x/abc123", "placeholder", author="Jane Dev <jane@example.com>"
    )

    author = subprocess.run(
        ["git", "log", "-1", "--pretty=%an <%ae>"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    committer = subprocess.run(
        ["git", "log", "-1", "--pretty=%cn <%ce>"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert author == "Jane Dev <jane@example.com>"
    # _git_id()'s default bot identity -- author= only overrides the Author field,
    # commit_to_branch's own -c user.name/user.email (the Committer) is untouched.
    assert committer == "sync-service[bot] <sync-service@users.noreply.github.com>"


def test_reword_commit_preserves_the_original_author(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("changed\n")
    publish.commit_to_branch(
        repo, "sync/x/abc123", "placeholder", author="Jane Dev <jane@example.com>"
    )

    publish.reword_commit(repo, "Sync x changes")

    author = subprocess.run(
        ["git", "log", "-1", "--pretty=%an <%ae>"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert author == "Jane Dev <jane@example.com>"  # --amend without --author keeps it


def test_reword_commit_changes_message_without_touching_the_tree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("changed\n")
    publish.commit_to_branch(repo, "sync/x/abc123", "mechanical placeholder")

    tree_before = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout

    publish.reword_commit(repo, "Sync x changes\n\nsync: x @ abc123")

    message = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    tree_after = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout

    assert message.strip() == "Sync x changes\n\nsync: x @ abc123"
    assert tree_after == tree_before  # amending the message never touches the actual content


def test_slugify_produces_a_safe_readable_branch_segment():
    assert publish.slugify("Sync portmon changes") == "sync-portmon-changes"
    assert publish.slugify("Fix: bug #42 (urgent!!)") == "fix-bug-42-urgent"


def test_slugify_falls_back_when_nothing_alphanumeric_survives():
    assert publish.slugify("!!!") == "change"
    assert publish.slugify("") == "change"


def test_slugify_caps_length_without_trailing_hyphen():
    slug = publish.slugify("a " * 40, max_length=10)
    assert len(slug) <= 10
    assert not slug.endswith("-")


def test_rename_branch_swaps_the_current_branch_name(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("changed\n")
    publish.commit_to_branch(repo, "sync/x/abc123", "mechanical placeholder")

    publish.rename_branch(repo, "sync/x/abc123-fix-the-thing")

    assert publish.branch_exists(repo, "sync/x/abc123-fix-the-thing")
    assert not publish.branch_exists(repo, "sync/x/abc123")


def test_already_synced_is_false_until_recorded(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("changed\n")
    publish.commit_to_branch(repo, "sync/x/abc123", "mechanical placeholder")
    publish.rename_branch(repo, "some-clean-title-slug")

    assert not publish.already_synced(repo, "x", "abc123def456")

    publish.record_synced(repo, "x", "abc123def456")

    # Keyed on (mapping_key, head_sha), not on the branch name at all -- the final
    # branch name carries no sha for this to match against anymore.
    assert publish.already_synced(repo, "x", "abc123def456")
    assert not publish.already_synced(repo, "y", "abc123def456")  # different mapping
    assert not publish.already_synced(repo, "x", "def456abc123")  # different sha


def test_record_synced_pushes_the_tracking_ref_when_a_remote_is_configured(tmp_path):
    # A fresh Action-runner checkout only knows what a previous run pushed, not what
    # a previous run recorded purely locally -- without pushing this ref too,
    # idempotency would silently stop working the moment this runs on CI instead of
    # a long-lived local clone. Mirrors branch_exists' own remote-check rationale.
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    pusher = tmp_path / "pusher"
    _init_repo(pusher)
    _git(pusher, "remote", "add", "origin", str(bare))
    _git(pusher, "push", "-q", "-u", "origin", "main")

    publish.record_synced(pusher, "x", "abc123def456")

    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(bare), str(fresh)], check=True)

    assert publish.already_synced(fresh, "x", "abc123def456") is True  # remote-only, not local
    assert publish.already_synced(fresh, "x", "does-not-exist") is False


def test_record_synced_is_local_only_without_a_remote(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    # No remote configured -- the dry-run scenario, same as the demo. Should not
    # raise just because there's nothing to push to.
    publish.record_synced(repo, "x", "abc123def456")
    assert publish.already_synced(repo, "x", "abc123def456")


def test_open_pr_fails_loudly_when_gh_missing_but_remote_configured(tmp_path, monkeypatch):
    # A remote being configured means a real publish was intended -- if `gh` isn't
    # available to do it, that must surface as a failure, never a silent dry-run
    # reported as success. Independent of whether `gh` happens to be installed
    # wherever this test runs: shutil.which is forced to report it as absent.
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", "/nonexistent/path/does-not-matter.git")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: None)

    result = publish.open_pr(repo, "sync/x/abc123", "main", "title", "body")

    assert result.success is False
    assert "gh" in result.message.lower()

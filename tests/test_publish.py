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

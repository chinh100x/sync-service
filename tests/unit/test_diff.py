import subprocess

from sync_service.lib.config import BreakCheck, Mapping
from sync_service.lib.diff import commit_author, commit_message, match


def test_commit_author_reads_name_and_email_from_the_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Jane Dev",
            "-c",
            "user.email=jane@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "x",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    assert commit_author(repo, sha) == "Jane Dev <jane@example.com>"


def test_commit_message_reads_the_full_raw_message(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "Fix the thing\n\nSome body text explaining why.",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    assert commit_message(repo, sha) == "Fix the thing\n\nSome body text explaining why."


def _mapping(key, source, dest="dest/"):
    return Mapping(
        key=key, source=source, dest=dest, break_check=BreakCheck(install="true", run="true")
    )


def test_match_hits_only_touched_mappings():
    mappings = [_mapping("portmon", "src/portmon/"), _mapping("tools", "src/tools/")]
    files = ["src/portmon/covenant.py", "README.md"]
    hits = match(files, mappings)
    assert hits == {"portmon": ["src/portmon/covenant.py"]}


def test_match_no_touch_is_empty():
    mappings = [_mapping("portmon", "src/portmon/")]
    hits = match(["README.md"], mappings)
    assert hits == {}


def test_match_multiple_files_same_mapping():
    mappings = [_mapping("portmon", "src/portmon/")]
    files = ["src/portmon/a.py", "src/portmon/b.py"]
    hits = match(files, mappings)
    assert hits["portmon"] == files


def test_match_whole_repo_mapping_matches_anything():
    mappings = [_mapping("portmon", ".")]
    files = [".github/workflows/sync.yaml", "README.md"]
    hits = match(files, mappings)
    assert hits["portmon"] == files


def test_match_default_source_is_whole_repo():
    m = Mapping(key="portmon", break_check=BreakCheck(install="true", run="true"))
    assert m.source == "." and m.dest == "."
    hits = match(["anything.py"], [m])
    assert hits == {"portmon": ["anything.py"]}

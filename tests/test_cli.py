import subprocess

from sync_service import cli, notify

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com"]

SENSITIVE_TEXT = "Rocky Mountain CAG SharePoint sync"


def _git(repo, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, capture_output=True, text=True, check=True)


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


def test_sensitive_commit_message_never_reaches_the_far_side(tmp_path, capsys):
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n")
    _write(
        prod,
        "sync/monitoring.yaml",
        "mappings:\n"
        "  - key: portmon\n"
        "    source: src/portmon\n"
        "    dest: plugin\n"
        "    break_check:\n"
        '      install: "true"\n'
        '      run: "true"\n',
    )
    base = _commit(prod, "initial")

    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    # The commit message itself is the leak vector under test -- it must never appear
    # verbatim in the far-side commit message or the PR title/dry-run output.
    head = _commit(prod, f"Fix {SENSITIVE_TEXT}\n\nCustomer uses /CO3/RockyMountain/IC/ internally.")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    cli.main(
        [
            "run",
            "--config", str(prod / "sync" / "monitoring.yaml"),
            "--source-repo", str(prod),
            "--dest-repo", str(oss),
            "--base", base,
            "--head", head,
        ]
    )

    printed = capsys.readouterr().out
    assert SENSITIVE_TEXT not in printed
    assert "RockyMountain" not in printed

    branch = f"sync/portmon/{head[:12]}"
    far_side_message = _git(oss, "log", "-1", "--pretty=%B", branch).stdout
    assert SENSITIVE_TEXT not in far_side_message
    assert "RockyMountain" not in far_side_message


def test_publish_failure_is_a_nonzero_exit_not_silent_success(tmp_path, monkeypatch):
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n")
    _write(
        prod,
        "sync/monitoring.yaml",
        "mappings:\n"
        "  - key: portmon\n"
        "    source: src/portmon\n"
        "    dest: plugin\n"
        "    break_check:\n"
        '      install: "true"\n'
        '      run: "true"\n',
    )
    base = _commit(prod, "initial")
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")
    # A remote that's configured but unreachable -- has_remote is True (so open_pr
    # takes the "real" path, not the dry-run print), but the push itself fails.
    _git(oss, "remote", "add", "origin", "/nonexistent/path/that/does/not/exist.git")

    slack_messages = []
    monkeypatch.setattr(notify.slack, "post", lambda text: slack_messages.append(text) or True)

    exit_code = cli.main(
        [
            "run",
            "--config", str(prod / "sync" / "monitoring.yaml"),
            "--source-repo", str(prod),
            "--dest-repo", str(oss),
            "--base", base,
            "--head", head,
        ]
    )

    assert exit_code == 1
    # Previously this outcome never reached notify.py at all -- fixed alongside
    # wiring in Slack, since a publish failure is exactly the kind of thing worth
    # a notification for.
    assert any("publish failed" in m for m in slack_messages)


def test_successful_pr_notifies_slack(tmp_path, monkeypatch):
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n")
    _write(
        prod,
        "sync/monitoring.yaml",
        "mappings:\n"
        "  - key: portmon\n"
        "    source: src/portmon\n"
        "    dest: plugin\n"
        "    break_check:\n"
        '      install: "true"\n'
        '      run: "true"\n',
    )
    base = _commit(prod, "initial")
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")
    # No remote configured -- the dry-run success path, same as the demo.

    slack_messages = []
    monkeypatch.setattr(notify.slack, "post", lambda text: slack_messages.append(text) or True)

    exit_code = cli.main(
        [
            "run",
            "--config", str(prod / "sync" / "monitoring.yaml"),
            "--source-repo", str(prod),
            "--dest-repo", str(oss),
            "--base", base,
            "--head", head,
        ]
    )

    assert exit_code == 0
    assert len(slack_messages) == 1
    assert "portmon" in slack_messages[0]
    assert "PR opened" in slack_messages[0]

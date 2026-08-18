import os
import subprocess

from sync_service import cli, notify

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com"]

SENSITIVE_TEXT = "Rocky Mountain CAG SharePoint sync"


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
    head = _commit(
        prod, f"Fix {SENSITIVE_TEXT}\n\nCustomer uses /CO3/RockyMountain/IC/ internally."
    )

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

    # DeterministicPRWriter's title ("Sync portmon changes", no LLM configured here)
    # slugifies to this branch name -- see publish.slugify and cli.py's rename_branch call.
    branch = "sync-portmon-changes"
    far_side_message = _git(oss, "log", "-1", "--pretty=%B", branch).stdout
    assert SENSITIVE_TEXT not in far_side_message
    assert "RockyMountain" not in far_side_message


def test_far_side_commit_subject_is_the_generated_title_not_the_mechanical_string(tmp_path):
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

    branch = "sync-portmon-changes"
    subject = _git(oss, "log", "-1", "--pretty=%s", branch).stdout.strip()
    full_message = _git(oss, "log", "-1", "--pretty=%B", branch).stdout

    # No LLM enabled here -- DeterministicPRWriter's title, not the old mechanical
    # "sync: portmon @ <sha>" subject line.
    assert subject == "Sync portmon changes"
    # The mechanical "sync: <key> @ <sha>" trailer is gone entirely (v19) -- the
    # commit message is just the title, full stop; the sha still lives in the
    # branch name itself.
    assert full_message.strip() == "Sync portmon changes"
    assert "sync: portmon @" not in full_message


def test_far_side_commit_author_credits_the_real_prod_committer(tmp_path):
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
    # A real developer identity, distinct from the fixture-wide GIT_ID -- this is
    # the identity that should end up crediting the far-side commit's Author.
    _git(prod, "add", "-A")
    subprocess.run(
        ["git", "-c", "user.name=Jane Dev", "-c", "user.email=jane@example.com",
         "commit", "-m", "change"],
        cwd=prod, capture_output=True, text=True, check=True,
    )
    head = _rev(prod)

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

    branch = "sync-portmon-changes"
    author = _git(oss, "log", "-1", "--pretty=%an <%ae>", branch).stdout.strip()
    committer = _git(oss, "log", "-1", "--pretty=%cn <%ce>", branch).stdout.strip()

    assert author == "Jane Dev <jane@example.com>"
    # Committer stays the bot identity -- the pipeline's involvement is still
    # visible even though the byline now names the real committer.
    assert committer == "sync-service[bot] <sync-service@users.noreply.github.com>"


def test_idempotent_rerun_is_recognized_via_the_sync_ref_not_the_branch_name(tmp_path, capsys):
    # The branch name alone can't answer "has this (mapping, head_sha) already been
    # proposed" -- it's a clean title-derived slug with no sha in it. This exercises
    # the replacement mechanism (publish.already_synced/record_synced) end to end
    # through cli.main(), not just at the publish.py unit level.
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

    args = [
        "run",
        "--config", str(prod / "sync" / "monitoring.yaml"),
        "--source-repo", str(prod),
        "--dest-repo", str(oss),
        "--base", base,
        "--head", head,
    ]

    cli.main(args)
    capsys.readouterr()  # discard the first run's output
    exit_code = cli.main(args)

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert "skipping (idempotent re-run)" in printed
    # Only one branch exists, from the first run -- the re-run didn't create a
    # second, differently-named one for the same (mapping, head_sha).
    branches = _git(oss, "branch", "--list").stdout
    assert branches.count("sync-portmon-changes") == 1


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


def test_project_name_replaces_mechanical_label_in_slack_messages(tmp_path, monkeypatch):
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
        '      run: "true"\n'
        "project_name: Prod\n",
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

    # cli.main() does os.environ.setdefault(...) for the commit identity -- unlike
    # monkeypatch.setenv, that mutation happens *during* the test body, after
    # monkeypatch has already taken its "current value" snapshot, so monkeypatch's
    # own teardown has nothing to revert here. Clean up directly so this doesn't
    # leak "Prod Sync Bot" into every later test's commit identity in this process.
    os.environ.pop("SYNC_SERVICE_COMMIT_NAME", None)
    os.environ.pop("SYNC_SERVICE_COMMIT_EMAIL", None)

    assert exit_code == 0
    assert len(slack_messages) == 1
    # "Prod" replaces the mechanical "sync:portmon" prefix entirely -- matches the
    # requested "[Prod] PR opened: ..." format, not "[Prod:portmon] ..." (dry-run
    # here since no remote is configured, same as the demo).
    assert slack_messages[0].startswith("[Prod] PR opened (dry-run):")
    assert "sync:portmon" not in slack_messages[0]

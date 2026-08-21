import os
import subprocess

from sync_service import sync
from sync_service.lib import llm_client, notify, safety_review
from sync_service.lib.config import BreakCheck, Mapping

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


def test_commit_message_reaches_the_far_side_when_llm_safety_review_is_disabled(tmp_path):
    # Deliberate tradeoff, not a regression: every replayed commit's real
    # message is used as-is once it clears secretscan + safety_review (see
    # _replay_one_commit). With llm_safety_review off, the only check left is
    # secretscan -- credential-shaped patterns only. A business-context leak
    # like a customer/deal name in prose is exactly the category secretscan
    # structurally can't catch; that's what llm_safety_review exists for. See
    # test_replay_commits_halts_the_whole_batch_when_a_later_message_is_unsafe
    # for the case where llm_safety_review IS enabled and catches this instead.
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
        "llm_safety_review:\n"
        "  enabled: false\n",
    )
    base = _commit(prod, "initial")

    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(
        prod, f"Fix {SENSITIVE_TEXT}\n\nCustomer uses /CO3/RockyMountain/IC/ internally."
    )

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    sync.main(
        [
            "run",
            "--config",
            str(prod / "sync" / "monitoring.yaml"),
            "--source-repo",
            str(prod),
            "--dest-repo",
            str(oss),
            "--base",
            base,
            "--head",
            head,
        ]
    )

    branch = "sync-portmon-changes"
    far_side_message = _git(oss, "log", "-1", "--pretty=%B", branch).stdout
    assert SENSITIVE_TEXT in far_side_message


def test_far_side_commit_keeps_its_own_message_pr_gets_a_generated_title_separately(tmp_path):
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
        "llm_safety_review:\n"
        "  enabled: false\n",
    )
    base = _commit(prod, "initial")
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    exit_code = sync.main(
        [
            "run",
            "--config",
            str(prod / "sync" / "monitoring.yaml"),
            "--source-repo",
            str(prod),
            "--dest-repo",
            str(oss),
            "--base",
            base,
            "--head",
            head,
        ]
    )

    branch = "sync-portmon-changes"
    subject = _git(oss, "log", "-1", "--pretty=%s", branch).stdout.strip()
    full_message = _git(oss, "log", "-1", "--pretty=%B", branch).stdout

    # The replayed commit keeps the real source commit's own message, full stop --
    # the generated title (DeterministicPRWriter's "Sync portmon changes", no LLM
    # configured here) is used for the PR itself only, never to overwrite it.
    assert exit_code == 0
    assert subject == "change"
    assert full_message.strip() == "change"


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
        '      run: "true"\n'
        "llm_safety_review:\n"
        "  enabled: false\n",
    )
    base = _commit(prod, "initial")
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    # A real developer identity, distinct from the fixture-wide GIT_ID -- this is
    # the identity that should end up crediting the far-side commit's Author.
    _git(prod, "add", "-A")
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Jane Dev",
            "-c",
            "user.email=jane@example.com",
            "commit",
            "-m",
            "change",
        ],
        cwd=prod,
        capture_output=True,
        text=True,
        check=True,
    )
    head = _rev(prod)

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    sync.main(
        [
            "run",
            "--config",
            str(prod / "sync" / "monitoring.yaml"),
            "--source-repo",
            str(prod),
            "--dest-repo",
            str(oss),
            "--base",
            base,
            "--head",
            head,
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
    # through sync.main(), not just at the publish.py unit level.
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
        "llm_safety_review:\n"
        "  enabled: false\n",
    )
    base = _commit(prod, "initial")
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    args = [
        "run",
        "--config",
        str(prod / "sync" / "monitoring.yaml"),
        "--source-repo",
        str(prod),
        "--dest-repo",
        str(oss),
        "--base",
        base,
        "--head",
        head,
    ]

    sync.main(args)
    capsys.readouterr()  # discard the first run's output
    exit_code = sync.main(args)

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert "skipping (idempotent re-run)" in printed
    # Only one branch exists, from the first run -- the re-run didn't create a
    # second, differently-named one for the same (mapping, head_sha).
    branches = _git(oss, "branch", "--list").stdout
    assert branches.count("sync-portmon-changes") == 1


def test_skipped_exists_notifies_slack_with_the_reason(tmp_path, monkeypatch):
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
        "llm_safety_review:\n"
        "  enabled: false\n",
    )
    base = _commit(prod, "initial")
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    args = [
        "run",
        "--config",
        str(prod / "sync" / "monitoring.yaml"),
        "--source-repo",
        str(prod),
        "--dest-repo",
        str(oss),
        "--base",
        base,
        "--head",
        head,
    ]

    sync.main(args)  # first run: opens the PR, no assertions needed here

    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)
    exit_code = sync.main(args)  # second run, same head_sha -- idempotent re-run

    assert exit_code == 0
    assert len(captured) == 1
    assert "idempotent re-run" in captured[0]


def test_a_commit_that_only_touches_excluded_files_is_a_no_op_not_a_halt(tmp_path, monkeypatch):
    # A commit touches mapping.source (so commits_between finds it and the loop
    # runs), but every path it changed is exclude-filtered inside
    # _replay_one_commit -- resolves as a no-op step, same as "unchanged", not
    # a distinct outcome. ("empty" is reserved for no commits touching
    # mapping.source at all -- see test_no_commits_under_source_notifies_empty.)
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
        # The only file under source is excluded.
        "    exclude: [src/portmon/covenant.py]\n"
        "    break_check:\n"
        '      install: "true"\n'
        '      run: "true"\n',
    )
    base = _commit(prod, "initial")
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    exit_code = sync.main(
        [
            "run",
            "--config",
            str(prod / "sync" / "monitoring.yaml"),
            "--source-repo",
            str(prod),
            "--dest-repo",
            str(oss),
            "--base",
            base,
            "--head",
            head,
        ]
    )

    assert exit_code == 0
    assert len(captured) == 1
    assert "nothing to commit" in captured[0]


def test_no_commits_under_source_notifies_empty(tmp_path, monkeypatch):
    # base == head is the simplest way to get an empty commit range. Calling
    # run_mapping() directly rather than through sync.main(): if diff.match's
    # top-level trigger genuinely found nothing touched, main() wouldn't call
    # run_mapping at all (a different, already-covered no-op path) -- this
    # test is specifically about run_mapping's own "empty" branch once it's
    # been invoked for a mapping with zero commits in range.
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n")
    base = _commit(prod, "initial")
    head = base  # no commits at all between base and head

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    mapping = Mapping(
        key="portmon",
        source="src/portmon",
        dest="plugin",
        break_check=BreakCheck(install="true", run="true"),
    )
    outcome = sync.run_mapping(
        mapping=mapping,
        source_repo=prod,
        dest_repo=oss,
        base_sha=base,
        head_sha=head,
        base_branch="main",
        gh_token=None,
        llm_pr_enabled=False,
        llm_safety_review_enabled=False,
        llm_safety_review_additional_context=None,
        project_name=None,
    )

    assert outcome == "empty"
    assert len(captured) == 1
    assert "no commits under src/portmon/" in captured[0]


def test_unchanged_notifies_slack_with_the_reason(tmp_path, monkeypatch):
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
        "llm_safety_review:\n"
        "  enabled: false\n",
    )
    base = _commit(prod, "initial")
    # A real change from base to head (git commit needs a real diff to succeed),
    # but the OSS side is pre-seeded with exactly what this change scrubs to --
    # so diff.match() fires, yet commit_all finds nothing new to commit.
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    _write(oss, "plugin/covenant.py", "def check():\n    return False\n")
    _commit(oss, "initial")

    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    exit_code = sync.main(
        [
            "run",
            "--config",
            str(prod / "sync" / "monitoring.yaml"),
            "--source-repo",
            str(prod),
            "--dest-repo",
            str(oss),
            "--base",
            base,
            "--head",
            head,
        ]
    )

    assert exit_code == 0
    assert len(captured) == 1
    assert "nothing to commit" in captured[0]


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
        '      run: "true"\n'
        "llm_safety_review:\n"
        "  enabled: false\n",
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

    exit_code = sync.main(
        [
            "run",
            "--config",
            str(prod / "sync" / "monitoring.yaml"),
            "--source-repo",
            str(prod),
            "--dest-repo",
            str(oss),
            "--base",
            base,
            "--head",
            head,
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
        '      run: "true"\n'
        "llm_safety_review:\n"
        "  enabled: false\n",
    )
    base = _commit(prod, "initial")
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")
    # No remote configured -- the dry-run success path, same as the demo.

    slack_messages = []
    monkeypatch.setattr(notify.slack, "post", lambda text: slack_messages.append(text) or True)

    exit_code = sync.main(
        [
            "run",
            "--config",
            str(prod / "sync" / "monitoring.yaml"),
            "--source-repo",
            str(prod),
            "--dest-repo",
            str(oss),
            "--base",
            base,
            "--head",
            head,
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
        "llm_safety_review:\n"
        "  enabled: false\n"
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

    exit_code = sync.main(
        [
            "run",
            "--config",
            str(prod / "sync" / "monitoring.yaml"),
            "--source-repo",
            str(prod),
            "--dest-repo",
            str(oss),
            "--base",
            base,
            "--head",
            head,
        ]
    )

    # sync.main() does os.environ.setdefault(...) for the commit identity -- unlike
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


# --- One OSS commit per source commit, keeping each one's own message/author,
# not one squashed commit per run. ---------------------------------------------


class _FakeResponse:
    def __init__(self, output_parsed):
        self.output_parsed = output_parsed


class _FakeResponses:
    def __init__(self, behavior):
        self._behavior = behavior

    def parse(self, **kwargs):
        return self._behavior(**kwargs)


class _FakeClient:
    def __init__(self, behavior, **_kwargs):
        self.responses = _FakeResponses(behavior)


def _fake_openai(monkeypatch, behavior):
    monkeypatch.setattr(llm_client, "OpenAI", lambda **_kwargs: _FakeClient(behavior))


def _passes_everything(**kw):
    return _FakeResponse(safety_review.SafetyVerdict(passed=True, categories=[], summary="fine"))


def _replay_config(*, llm_safety_review_enabled=False):
    return (
        "mappings:\n"
        "  - key: portmon\n"
        "    source: src/portmon\n"
        "    dest: plugin\n"
        "    break_check:\n"
        '      install: "true"\n'
        '      run: "true"\n'
        "llm_pr:\n"
        "  enabled: false\n"
        "llm_safety_review:\n"
        f"  enabled: {str(llm_safety_review_enabled).lower()}\n"
    )


def _run(prod, oss, base, head):
    return sync.main(
        [
            "run",
            "--config",
            str(prod / "sync" / "monitoring.yaml"),
            "--source-repo",
            str(prod),
            "--dest-repo",
            str(oss),
            "--base",
            base,
            "--head",
            head,
        ]
    )


def test_replay_commits_produces_one_oss_commit_per_source_commit_with_original_messages(
    tmp_path,
):
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "sync/monitoring.yaml", _replay_config())
    base = _commit(prod, "initial")

    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n")
    _commit(prod, "Add covenant check")
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    second = _commit(prod, "Flip covenant default")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    exit_code = _run(prod, oss, base, second)

    assert exit_code == 0
    branch = "sync-portmon-changes"
    messages = _git(oss, "log", "--format=%s", f"main..{branch}").stdout.splitlines()
    # Oldest first in history, so `git log`'s newest-first order lists them reversed.
    assert messages == ["Flip covenant default", "Add covenant check"]
    assert (oss / "plugin" / "covenant.py").exists()


def test_replay_commits_propagates_a_deletion(tmp_path):
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "sync/monitoring.yaml", _replay_config())
    base = _commit(prod, "initial")

    _write(prod, "src/portmon/a.py", "keep\n")
    _write(prod, "src/portmon/b.py", "remove me\n")
    _commit(prod, "add a and b")
    (prod / "src" / "portmon" / "b.py").unlink()
    head = _commit(prod, "remove b, it's unused")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    exit_code = _run(prod, oss, base, head)

    assert exit_code == 0
    branch = "sync-portmon-changes"
    _git(oss, "checkout", branch)
    assert (oss / "plugin" / "a.py").exists()
    assert not (oss / "plugin" / "b.py").exists()


def test_replay_commits_halts_the_whole_batch_when_a_later_message_is_unsafe(tmp_path, monkeypatch):
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "sync/monitoring.yaml", _replay_config(llm_safety_review_enabled=True))
    base = _commit(prod, "initial")

    _write(prod, "src/portmon/a.py", "1\n")
    _commit(prod, "clean first commit")
    _write(prod, "src/portmon/a.py", "2\n")
    # The leak is in the MESSAGE, not the file content -- the file-content review
    # must pass for both commits; only the message check on this second commit
    # should ever see a block.
    head = _commit(prod, "Fix Rocky Mountain CAG SharePoint sync\n\ninternal detail")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    def behavior(**kw):
        user_content = kw["input"][1]["content"]
        if "Rocky Mountain" in user_content:
            return _FakeResponse(
                safety_review.SafetyVerdict(
                    passed=False, categories=["customer_name"], summary="names a customer"
                )
            )
        return _passes_everything(**kw)

    _fake_openai(monkeypatch, behavior)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    exit_code = _run(prod, oss, base, head)

    assert exit_code == 0  # correct policy enforcement, not a tool failure
    # Nothing partial reached origin -- not even the first, individually-clean
    # commit's own branch/content survives a later halt in the same batch.
    assert not _git(oss, "branch", "--list", "sync-portmon-*").stdout.strip()
    # git checkout can leave an empty "plugin/" directory sitting on disk (git
    # doesn't track/clean up empty dirs) -- what actually matters is that main
    # never gained a tracked file there.
    assert "plugin/a.py" not in _git(oss, "ls-files").stdout
    assert any("replay halted" in m and "commit message" in m for m in captured)


def test_replay_commits_secret_in_message_halts_before_any_commit_survives(tmp_path):
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "sync/monitoring.yaml", _replay_config())
    base = _commit(prod, "initial")

    _write(prod, "src/portmon/a.py", "1\n")
    key_msg = 'AKIA_KEY = "AKIAABCDEFGHIJKLMNOP"'  # pragma: allowlist secret
    head = _commit(prod, key_msg)

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    exit_code = _run(prod, oss, base, head)

    assert exit_code == 0
    assert not _git(oss, "branch", "--list", "sync-portmon-*").stdout.strip()
    assert "plugin/a.py" not in _git(oss, "ls-files").stdout


def test_replay_commits_never_leaves_a_raw_pre_redaction_blob_in_the_oss_git_database(
    tmp_path,
):
    # End-to-end version of patch.py's own object-database test: a real
    # redact rule fires during a real replay run through sync.main(), and no
    # object anywhere in the OSS repo's git database -- not just the final
    # commit's tree, ANY object at all, including ones a naive `git apply
    # --index` would have left dangling -- may ever contain the raw value.
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    config = (
        "mappings:\n"
        "  - key: portmon\n"
        "    source: src/portmon\n"
        "    dest: plugin\n"
        "    redact:\n"
        "      - pattern: RAW_SENSITIVE_MARKER\n"
        "        replace: <REDACTED>\n"
        "    break_check:\n"
        '      install: "true"\n'
        '      run: "true"\n'
        "llm_pr:\n"
        "  enabled: false\n"
        "llm_safety_review:\n"
        "  enabled: false\n"
    )
    _write(prod, "sync/monitoring.yaml", config)
    base = _commit(prod, "initial")

    _write(prod, "src/portmon/a.py", "RAW_SENSITIVE_MARKER\n")
    head = _commit(prod, "add a.py")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    exit_code = _run(prod, oss, base, head)

    assert exit_code == 0
    assert (oss / "plugin" / "a.py").read_text() == "<REDACTED>\n"

    all_blobs = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"],
        cwd=oss,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in all_blobs.splitlines():
        sha, kind = line.split()
        if kind != "blob":
            continue
        content = subprocess.run(
            ["git", "cat-file", "-p", sha], cwd=oss, capture_output=True, text=True, check=True
        ).stdout
        assert "RAW_SENSITIVE_MARKER" not in content


def test_replay_commits_overwrites_a_divergent_oss_file_with_no_halt(tmp_path):
    # No divergence detection in replay mode either -- matches the snapshot
    # path's own already-documented tradeoff (README's very first lines): a
    # run always overwrites, unconditionally, whether or not the OSS side had
    # its own independent content sitting at that path. An earlier version of
    # this pipeline applied a raw unified diff via `git apply`, where this
    # exact scenario happened to fail (a "create" patch can't apply on top of
    # existing unrelated content) and halted -- an accidental, incomplete side
    # effect of patch mechanics, not a deliberate divergence-detection feature
    # (see plan.md's own separate, not-yet-built "Phase 2 -- divergence
    # detection"). Reading and writing content directly removes that
    # incidental protection along with the parsing complexity that came with
    # it -- replay mode's guarantees now match what's already documented.
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "sync/monitoring.yaml", _replay_config())
    base = _commit(prod, "initial")
    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n")
    head = _commit(prod, "Add covenant check")

    _write(oss, "README.md", "# oss\n")
    _write(oss, "plugin/covenant.py", "# hand-written placeholder, not from prod\n")
    _commit(oss, "initial")

    exit_code = _run(prod, oss, base, head)

    assert exit_code == 0
    branch = "sync-portmon-changes"
    assert _git(oss, "branch", "--list", branch).stdout.strip()
    _git(oss, "checkout", branch)
    assert (oss / "plugin" / "covenant.py").read_text() == "def check():\n    return True\n"


def test_replay_commits_halts_on_a_submodule_with_nothing_committed(tmp_path):
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    other = tmp_path / "other"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _write(other, "README.md", "vendored dependency\n")
    _commit(other, "initial")

    _write(prod, "sync/monitoring.yaml", _replay_config())
    base = _commit(prod, "initial")
    _git(
        prod,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(other),
        "src/portmon/vendor",
    )
    head = _commit(prod, "add a submodule")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    exit_code = _run(prod, oss, base, head)

    assert exit_code == 0
    assert not _git(oss, "branch", "--list", "sync-portmon-*").stdout.strip()


def test_replay_commits_propagates_binary_content_unscrubbed_and_discloses_it(tmp_path, capsys):
    # No mechanical way to redact binary content (regex doesn't apply to
    # bytes) or run safety_review on it (nothing textual to judge) -- it
    # propagates as-is rather than being dropped, with the tradeoff disclosed
    # in the PR body (never silent) and in the run's own log output.
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "sync/monitoring.yaml", _replay_config())
    base = _commit(prod, "initial")
    _write(prod, "src/portmon/a.py", "1\n")
    raw = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    (prod / "src" / "portmon" / "img.png").write_bytes(raw)
    head = _commit(prod, "add a text file and a binary file")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    exit_code = _run(prod, oss, base, head)
    printed = capsys.readouterr().out

    assert exit_code == 0
    branch = "sync-portmon-changes"
    _git(oss, "checkout", branch)
    assert (oss / "plugin" / "a.py").read_text() == "1\n"
    assert (oss / "plugin" / "img.png").read_bytes() == raw  # propagated byte-for-byte
    assert "propagating 1 binary file(s)" in printed
    assert "## Binary Files (Not Scanned)" in printed
    assert "plugin/img.png" in printed


def test_replay_commits_still_secretscans_binary_content_via_a_latin1_view(tmp_path):
    # Binary content still joins the secret-scan gate -- an ASCII
    # credential-shaped substring embedded in otherwise-binary bytes is still
    # catchable via a lossless latin-1 decode, even without valid UTF-8.
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    _write(prod, "sync/monitoring.yaml", _replay_config())
    base = _commit(prod, "initial")
    fake_key = b"AKIAABCDEFGHIJKLMNOP"  # pragma: allowlist secret
    (prod / "src" / "portmon").mkdir(parents=True)
    (prod / "src" / "portmon" / "asset.bin").write_bytes(b"\x89\x00\x01" + fake_key + b"\x02\xff")
    head = _commit(prod, "add a binary asset with an embedded key-shaped string")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    exit_code = _run(prod, oss, base, head)

    assert exit_code == 0
    assert not _git(oss, "branch", "--list", "sync-portmon-*").stdout.strip()
    assert "plugin/asset.bin" not in _git(oss, "ls-files").stdout

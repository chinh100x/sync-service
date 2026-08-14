"""End-to-end coverage through cli.main() for scenarios no other test file
exercises at the orchestration level: multiple mappings in one run, the
no-divergence-tracking overwrite behavior, a break-check halt/revert/retry
cycle, and a no-op run. Replaces demo/run_demo.py, which only printed these
for a human to eyeball -- this asserts on them instead, so a regression
actually fails CI.
"""
import re
import subprocess

from sync_service import cli

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com"]

_BRANCH_RE = re.compile(r"Would open PR (\S+) ->")

_CONFIG = """\
mappings:
  - key: portmon
    source: src/portmon
    dest: plugin
    exclude:
      - src/portmon/internal_reporting.py
    redact:
      - pattern: 'https://cag-mcp\\.internal[^\\s"]*'
        replace: '<MCP_ENDPOINT>'
    break_check:
      install: "true"
      run: "true"
  - key: brk
    source: src/brk
    dest: brk
    break_check:
      install: "true"
      run: "python3 brk/mod.py"
"""


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


def _init_pair(tmp_path):
    prod, oss = tmp_path / "prod", tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")
    _write(prod, "sync/monitoring.yaml", _CONFIG)
    return prod, oss


def _run(prod, oss, base, head):
    return cli.main(
        [
            "run",
            "--config", str(prod / "sync" / "monitoring.yaml"),
            "--source-repo", str(prod),
            "--dest-repo", str(oss),
            "--base", base,
            "--head", head,
        ]
    )


def _files_on_branch(repo, branch):
    return set(_git(repo, "ls-tree", "-r", "--name-only", branch).stdout.splitlines())


def test_single_run_touching_two_mappings_opens_a_pr_for_each(tmp_path, capsys):
    prod, oss = _init_pair(tmp_path)
    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n")
    base = _commit(prod, "initial")
    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    _write(
        prod,
        "src/portmon/covenant.py",
        'ENDPOINT = "https://cag-mcp.internal/v1/report"\n\ndef check():\n    return True\n',
    )
    _write(prod, "src/portmon/internal_reporting.py", "SECRET_TENANT_ID = 'do-not-ship'\n")
    _write(prod, "src/brk/mod.py", "def run():\n    print('ok')\n\nif __name__ == '__main__':\n    run()\n")
    head = _commit(prod, "portmon+brk changes")

    exit_code = _run(prod, oss, base, head)
    printed = capsys.readouterr().out

    assert exit_code == 0
    branches = _BRANCH_RE.findall(printed)
    assert len(branches) == 2  # one PR per mapping touched, not one for the whole commit

    files_by_branch = {b: _files_on_branch(oss, b) for b in branches}
    portmon_branch = next(b for b, files in files_by_branch.items() if "plugin/covenant.py" in files)
    brk_branch = next(b for b, files in files_by_branch.items() if "brk/mod.py" in files)

    assert "plugin/internal_reporting.py" not in files_by_branch[portmon_branch]  # excluded
    portmon_text = _git(oss, "show", f"{portmon_branch}:plugin/covenant.py").stdout
    assert "<MCP_ENDPOINT>" in portmon_text and "cag-mcp.internal" not in portmon_text  # redacted
    assert "brk/mod.py" in files_by_branch[brk_branch]


def test_outside_edit_is_silently_overwritten_by_the_next_forward_sync(tmp_path, capsys):
    # No manifest / divergence detection: a forward sync always overwrites the far
    # side's tracked files with the near side's current content, even if an outside
    # contributor edited that file on the far side in between. Deliberate tradeoff,
    # not a bug -- this pins the behavior down so it can't silently regress either way.
    prod, oss = _init_pair(tmp_path)
    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n")
    base = _commit(prod, "initial")
    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n\ndef audit():\n    return 'ok'\n")
    head = _commit(prod, "portmon: add audit()")
    exit_code = _run(prod, oss, base, head)
    branch = _BRANCH_RE.findall(capsys.readouterr().out)[0]
    _git(oss, "checkout", "main")
    _git(oss, "merge", "--no-ff", branch, "-m", "merge sync PR")
    assert exit_code == 0

    # an outside contributor edits the propagated file directly on oss main
    _write(oss, "plugin/covenant.py", "def check():\n    return True\n\n# outsider's note\n")
    _commit(oss, "outside PR: add a note")

    # prod changes the same file again, independently
    next_base = head
    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n\ndef audit():\n    return 'ok'\n\n# prod tweak\n")
    next_head = _commit(prod, "portmon: another tweak")
    exit_code = _run(prod, oss, next_base, next_head)
    branch = _BRANCH_RE.findall(capsys.readouterr().out)[0]

    landed = _git(oss, "show", f"{branch}:plugin/covenant.py").stdout
    assert exit_code == 0
    assert "outsider's note" not in landed  # silently clobbered, not merged or rejected
    assert "prod tweak" in landed


def test_breakcheck_failure_halts_reverts_and_succeeds_on_retry(tmp_path, capsys):
    prod, oss = _init_pair(tmp_path)
    _write(prod, "src/brk/mod.py", "def run():\n    print('ok')\n\nif __name__ == '__main__':\n    run()\n")
    base = _commit(prod, "initial")
    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    _write(prod, "src/brk/mod.py", "def run():\n    raise RuntimeError('boom')\n\nif __name__ == '__main__':\n    run()\n")
    head = _commit(prod, "brk: introduce a bug")
    exit_code = _run(prod, oss, base, head)
    printed = capsys.readouterr().out

    assert exit_code == 0  # a correctly-enforced halt, not a tool failure
    assert "break check failed" in printed
    assert _BRANCH_RE.findall(printed) == []  # no PR opened
    assert _git(oss, "status", "--porcelain").stdout == ""  # working tree reverted, nothing left dangling

    _write(prod, "src/brk/mod.py", "def run():\n    print('fixed')\n\nif __name__ == '__main__':\n    run()\n")
    retry_head = _commit(prod, "brk: fix the bug")
    exit_code = _run(prod, oss, head, retry_head)
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert len(_BRANCH_RE.findall(printed)) == 1  # retry succeeds once the bug is fixed


def test_commit_touching_no_mapping_is_a_noop(tmp_path, capsys):
    prod, oss = _init_pair(tmp_path)
    _write(prod, "src/portmon/covenant.py", "def check():\n    return True\n")
    base = _commit(prod, "initial")
    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")

    _write(prod, "README.md", "# production repo\nupdated\n")
    head = _commit(prod, "docs: update README")
    exit_code = _run(prod, oss, base, head)
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert "no mapping touched" in printed
    assert _BRANCH_RE.findall(printed) == []

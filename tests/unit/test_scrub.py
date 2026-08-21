import subprocess
from pathlib import Path

from sync_service.lib.config import RedactRule
from sync_service.lib.scrub import apply, redact_text

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com"]


def _git(repo, *args):
    subprocess.run(["git", *GIT_ID, *args], cwd=repo, check=True, capture_output=True)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _init_and_commit(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")


def test_exclude_drops_file(tmp_path):
    _write(tmp_path, "src/portmon/covenant.py", "print('ok')\n")
    _write(tmp_path, "src/portmon/internal_reporting.py", "print('internal')\n")
    _init_and_commit(tmp_path)

    desired, categories = apply(
        tmp_path,
        "src/portmon",
        "plugin",
        exclude=["src/portmon/internal_reporting.py"],
        transform_rules=[],
    )
    assert "plugin/covenant.py" in desired
    assert "plugin/internal_reporting.py" not in desired
    assert categories == []


def test_redact_replaces_pattern(tmp_path):
    _write(tmp_path, "src/portmon/covenant.py", 'ENDPOINT = "https://cag-mcp.internal/v1/x"\n')
    _init_and_commit(tmp_path)

    rule = RedactRule(pattern=r"https://cag-mcp\.internal[^\s\"]*", replace="<MCP_ENDPOINT>")
    desired, categories = apply(
        tmp_path, "src/portmon", "plugin", exclude=[], transform_rules=[rule]
    )
    assert "<MCP_ENDPOINT>" in desired["plugin/covenant.py"]
    assert "cag-mcp.internal" not in desired["plugin/covenant.py"]
    assert categories == []  # rule fired but has no `category` label


def test_redact_reports_triggered_category_only_when_a_rule_actually_fires(tmp_path):
    _write(tmp_path, "src/portmon/covenant.py", 'ENDPOINT = "https://cag-mcp.internal/v1/x"\n')
    _write(tmp_path, "src/portmon/other.py", "no endpoint here\n")
    _init_and_commit(tmp_path)

    fired = RedactRule(
        pattern=r"https://cag-mcp\.internal[^\s\"]*",
        replace="<MCP_ENDPOINT>",
        category="internal_endpoint",
    )
    never_fires = RedactRule(
        pattern=r"NEVER_PRESENT_TOKEN", replace="<X>", category="tenant_config"
    )
    desired, categories = apply(
        tmp_path, "src/portmon", "plugin", exclude=[], transform_rules=[fired, never_fires]
    )
    # never_fires's category is absent -- it never matched anything
    assert categories == ["internal_endpoint"]


def test_whole_repo_mapping_never_walks_sync_bookkeeping_dirs(tmp_path):
    # A full-repo mapping (source=".") must never propagate the tool's own manifest
    # or the target checkout action.yml places inside the workspace, even if either
    # were ever accidentally committed -- regardless of the mapping's own exclude
    # list (empty here on purpose). `.git` itself needs no equivalent case: `git
    # ls-files` can never return anything under it, by construction.
    _write(tmp_path, "README.md", "hello\n")
    _write(tmp_path, ".sync-state/portmon.json", "{}\n")
    _write(tmp_path, ".sync-service-target/README.md", "the other repo's own content\n")
    _init_and_commit(tmp_path)

    desired, _categories = apply(tmp_path, ".", ".", exclude=[], transform_rules=[])

    assert desired == {"README.md": "hello\n"}


def test_untracked_and_gitignored_files_never_propagate(tmp_path):
    # The real incident this guards against: a stale .pytest_cache/ directory (or
    # any build artifact/cache) sitting in the working tree at sync time used to
    # get swept in by a raw filesystem walk, even though the source repo's own
    # .gitignore already excludes it from the repo entirely. Walking `git
    # ls-files` instead means untracked/gitignored content is never a candidate
    # in the first place -- not caught downstream by exclude/redact/secretscan,
    # simply never seen.
    _write(tmp_path, ".gitignore", "__pycache__/\n*.pyc\n")
    _write(tmp_path, "src/app.py", "def version():\n    return '1.0'\n")
    _init_and_commit(tmp_path)

    # Untracked but not gitignored -- e.g. a file someone is about to add but hasn't yet.
    _write(tmp_path, "src/scratch.py", "# work in progress, never committed\n")
    # Gitignored build artifact, never tracked at all.
    _write(tmp_path, "src/__pycache__/app.cpython-313.pyc", "covenant_threshold = 42\n")

    desired, _categories = apply(tmp_path, ".", ".", exclude=[], transform_rules=[])

    assert set(desired) == {".gitignore", "src/app.py"}


# --- redact_text(): the same substitution, against a plain string -- what
# patch.py's per-commit replay path redacts in memory before ever writing
# anything into dest_repo, instead of a fresh source-tree walk -------------------


def test_redact_text_rewrites_a_matching_value():
    rule = RedactRule(pattern=r"https://cag-mcp\.internal[^\s\"]*", replace="<MCP_ENDPOINT>")

    text, categories = redact_text('ENDPOINT = "https://cag-mcp.internal/v1/x"\n', [rule])

    assert text == 'ENDPOINT = "<MCP_ENDPOINT>"\n'
    assert categories == []  # rule fired but has no `category` label


def test_redact_text_reports_triggered_category_only_when_a_rule_actually_fires():
    fired = RedactRule(
        pattern=r"https://cag-mcp\.internal[^\s\"]*",
        replace="<MCP_ENDPOINT>",
        category="internal_endpoint",
    )
    never_fires = RedactRule(
        pattern=r"NEVER_PRESENT_TOKEN", replace="<X>", category="tenant_config"
    )

    text, categories = redact_text(
        'ENDPOINT = "https://cag-mcp.internal/v1/x"\n', [fired, never_fires]
    )

    assert categories == ["internal_endpoint"]
    assert "<MCP_ENDPOINT>" in text

from pathlib import Path

from sync_service.config import RedactRule
from sync_service.scrub import apply


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_exclude_drops_file(tmp_path):
    _write(tmp_path, "src/portmon/covenant.py", "print('ok')\n")
    _write(tmp_path, "src/portmon/internal_reporting.py", "print('internal')\n")
    desired = apply(
        tmp_path,
        "src/portmon",
        "plugin",
        exclude=["src/portmon/internal_reporting.py"],
        transform_rules=[],
    )
    assert "plugin/covenant.py" in desired
    assert "plugin/internal_reporting.py" not in desired


def test_redact_replaces_pattern(tmp_path):
    _write(tmp_path, "src/portmon/covenant.py", 'ENDPOINT = "https://cag-mcp.internal/v1/x"\n')
    rule = RedactRule(pattern=r"https://cag-mcp\.internal[^\s\"]*", replace="<MCP_ENDPOINT>")
    desired = apply(tmp_path, "src/portmon", "plugin", exclude=[], transform_rules=[rule])
    assert "<MCP_ENDPOINT>" in desired["plugin/covenant.py"]
    assert "cag-mcp.internal" not in desired["plugin/covenant.py"]


def test_hydrate_reverses_a_redact_style_rule(tmp_path):
    # same mechanism, opposite direction: placeholder -> real value
    _write(tmp_path, "plugin/covenant.py", 'ENDPOINT = "<MCP_ENDPOINT>"\n')
    rule = RedactRule(pattern=r"<MCP_ENDPOINT>", replace="https://cag-mcp.internal/v1/report")
    desired = apply(tmp_path, "plugin", "src/portmon", exclude=[], transform_rules=[rule])
    assert "https://cag-mcp.internal/v1/report" in desired["src/portmon/covenant.py"]


def test_whole_repo_mapping_never_walks_mechanical_dirs(tmp_path):
    # A full-repo mapping (source=".") must never propagate git internals, the tool's
    # own manifest, or the counterpart checkout action.yml places inside the workspace —
    # regardless of the mapping's own exclude list (empty here on purpose).
    _write(tmp_path, "README.md", "hello\n")
    _write(tmp_path, ".git/HEAD", "ref: refs/heads/main\n")
    _write(tmp_path, ".sync-state/portmon.json", "{}\n")
    _write(tmp_path, ".sync-service-counterpart/README.md", "the other repo's own content\n")

    desired = apply(tmp_path, ".", ".", exclude=[], transform_rules=[])

    assert desired == {"README.md": "hello\n"}

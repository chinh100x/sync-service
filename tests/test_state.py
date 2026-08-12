from sync_service import state

BASE = 'ENDPOINT = "https://cag-mcp.internal/v1/report"\n\ndef check():\n    print("checked against", ENDPOINT)\n    return True\n'


def test_classify_matches_when_content_unchanged(tmp_path):
    (tmp_path / "plugin").mkdir()
    (tmp_path / "plugin" / "covenant.py").write_text(BASE)
    desired = {"plugin/covenant.py": BASE}
    state.write(tmp_path, "portmon", "sha1", desired)

    manifest = state.load(tmp_path, "portmon")
    result = state.classify(tmp_path, manifest, desired, "portmon")
    assert result["plugin/covenant.py"].status == "clean"


def test_classify_new_file_is_not_a_conflict(tmp_path):
    manifest = state.load(tmp_path, "portmon")  # no prior sync
    desired = {"plugin/new_file.py": "print('new')\n"}
    result = state.classify(tmp_path, manifest, desired, "portmon")
    assert result["plugin/new_file.py"].status == "new"


def test_classify_auto_merges_non_overlapping_edits(tmp_path):
    (tmp_path / "plugin").mkdir()
    (tmp_path / "plugin" / "covenant.py").write_text(BASE)
    state.write(tmp_path, "portmon", "sha1", {"plugin/covenant.py": BASE})

    # outside contributor edits a comment on OSS main, unrelated to the next prod change
    outsider_version = BASE.replace("def check():", "# outsider added a comment\ndef check():")
    (tmp_path / "plugin" / "covenant.py").write_text(outsider_version)

    manifest = state.load(tmp_path, "portmon")
    prod_version = BASE + "\ndef extra():\n    return 42\n"
    desired = {"plugin/covenant.py": prod_version}
    result = state.classify(tmp_path, manifest, desired, "portmon")

    entry = result["plugin/covenant.py"]
    assert entry.status == "merged"
    assert "outsider added a comment" in entry.write_content
    assert "def extra():" in entry.write_content


def test_classify_flags_real_conflict_when_same_region_edited_both_sides(tmp_path):
    (tmp_path / "plugin").mkdir()
    (tmp_path / "plugin" / "covenant.py").write_text(BASE)
    state.write(tmp_path, "portmon", "sha1", {"plugin/covenant.py": BASE})

    # outside contributor AND production both change the same line
    (tmp_path / "plugin" / "covenant.py").write_text(BASE.replace("return True", "return False  # outsider"))

    manifest = state.load(tmp_path, "portmon")
    desired = {"plugin/covenant.py": BASE.replace("return True", "return 'ok'  # prod")}
    result = state.classify(tmp_path, manifest, desired, "portmon")

    entry = result["plugin/covenant.py"]
    assert entry.status == "conflict"
    assert entry.write_content is None

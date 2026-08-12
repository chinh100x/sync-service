from sync_service import state

BASE = 'ENDPOINT = "https://cag-mcp.internal/v1/report"\n\ndef check():\n    print("checked against", ENDPOINT)\n    return True\n'


def test_classify_matches_when_content_unchanged(tmp_path):
    (tmp_path / "plugin").mkdir()
    (tmp_path / "plugin" / "covenant.py").write_text(BASE)
    desired = {"plugin/covenant.py": BASE}
    state.write(tmp_path, "portmon", "sha1", desired)

    manifest = state.load(tmp_path, "portmon")
    result = state.classify(tmp_path, manifest, desired)
    assert result["plugin/covenant.py"].status == "clean"


def test_classify_new_file_is_not_a_conflict(tmp_path):
    manifest = state.load(tmp_path, "portmon")  # no prior sync
    desired = {"plugin/new_file.py": "print('new')\n"}
    result = state.classify(tmp_path, manifest, desired)
    assert result["plugin/new_file.py"].status == "new"


def test_classify_flags_conflict_on_any_divergence(tmp_path):
    # Even a non-overlapping edit is a conflict now — no merge is attempted.
    (tmp_path / "plugin").mkdir()
    (tmp_path / "plugin" / "covenant.py").write_text(BASE)
    state.write(tmp_path, "portmon", "sha1", {"plugin/covenant.py": BASE})

    outsider_version = BASE.replace("def check():", "# outsider added a comment\ndef check():")
    (tmp_path / "plugin" / "covenant.py").write_text(outsider_version)

    manifest = state.load(tmp_path, "portmon")
    desired = {"plugin/covenant.py": BASE + "\ndef extra():\n    return 42\n"}
    result = state.classify(tmp_path, manifest, desired)

    entry = result["plugin/covenant.py"]
    assert entry.status == "conflict"
    assert entry.write_content is None


def test_classify_flags_conflict_for_untracked_existing_file(tmp_path):
    # Exists on the far side, but we have no manifest entry for it at all.
    (tmp_path / "plugin").mkdir()
    (tmp_path / "plugin" / "covenant.py").write_text("pre-existing, never synced\n")

    manifest = state.load(tmp_path, "portmon")  # no prior sync
    desired = {"plugin/covenant.py": "new content\n"}
    result = state.classify(tmp_path, manifest, desired)

    assert result["plugin/covenant.py"].status == "conflict"

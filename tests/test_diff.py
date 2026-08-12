from sync_service.config import BreakCheck, Mapping
from sync_service.diff import match


def _mapping(key, source, dest="dest/"):
    return Mapping(key=key, source=source, dest=dest, break_check=BreakCheck(install="true", run="true"))


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

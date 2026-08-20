import os

from sync_service.lib.breakcheck import run
from sync_service.lib.config import BreakCheck


def test_passes_when_both_steps_succeed(tmp_path):
    check = BreakCheck(install="true", run="true")
    result = run(tmp_path, check)
    assert result.passed is True


def test_fails_and_names_the_failed_step(tmp_path):
    check = BreakCheck(install="true", run="false")
    result = run(tmp_path, check)
    assert result.passed is False
    assert result.failed_step == "run"


def test_install_and_run_never_see_gh_token(tmp_path, monkeypatch):
    # Even if GH_TOKEN is sitting in this process's own environment, break_check's
    # install/run commands -- which execute far-side, possibly untrusted content --
    # must never inherit it.
    monkeypatch.setenv("GH_TOKEN", "should-never-be-visible")
    probe = "python3 -c \"import os,sys; sys.exit(1 if 'GH_TOKEN' in os.environ else 0)\""
    check = BreakCheck(install="true", run=probe)

    result = run(tmp_path, check)

    assert result.passed is True
    assert os.environ["GH_TOKEN"] == "should-never-be-visible"  # untouched outside the call

import subprocess

from sync_service import cli, llm_client, safety_review

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com"]


def _git(repo, *args):
    return subprocess.run(
        ["git", *GIT_ID, *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _make_context() -> safety_review.SafetyReviewContext:
    return safety_review.SafetyReviewContext(
        mapping_key="portmon", files={"plugin/covenant.py": "def check():\n    return True\n"}
    )


# --- fake OpenAI client -----------------------------------------------------------


class _FakeResponse:
    def __init__(self, output_parsed):
        self.output_parsed = output_parsed


class _FakeResponses:
    def __init__(self, behavior, calls):
        self._behavior = behavior
        self._calls = calls

    def parse(self, **kwargs):
        self._calls.append(kwargs)
        return self._behavior(**kwargs)


class _FakeClient:
    def __init__(self, behavior, calls, **_kwargs):
        self.responses = _FakeResponses(behavior, calls)


def _fake_openai(monkeypatch, behavior, calls=None):
    calls = calls if calls is not None else []

    def factory(**_kwargs):
        return _FakeClient(behavior, calls)

    monkeypatch.setattr(llm_client, "OpenAI", factory)
    return calls


def _raising_openai(monkeypatch):
    def factory(**_kwargs):
        raise AssertionError("OpenAI() must not be constructed for this scenario")

    monkeypatch.setattr(llm_client, "OpenAI", factory)


# --- _build_system_prompt: additive only, never a replacement ---------------------

def test_build_system_prompt_with_no_additional_context_is_just_the_base():
    prompt = safety_review._build_system_prompt(None)
    assert prompt == f"{safety_review._SYSTEM_PROMPT}\n\n{safety_review._RETURN_INSTRUCTION}"


def test_build_system_prompt_appends_additional_context_after_the_base():
    prompt = safety_review._build_system_prompt("Also flag covenant threshold values.")

    # The full base prompt survives untouched -- appending never edits or removes
    # any of its existing safety instructions.
    assert safety_review._SYSTEM_PROMPT in prompt
    assert "Also flag covenant threshold values." in prompt
    # The final "return structured output" instruction still comes last, after the
    # custom addition -- it isn't buried in the middle of the appended text.
    assert prompt.rstrip().endswith(safety_review._RETURN_INSTRUCTION)
    assert prompt.index(safety_review._SYSTEM_PROMPT) < prompt.index("Also flag covenant")
    assert prompt.index("Also flag covenant") < prompt.index(safety_review._RETURN_INSTRUCTION)


def test_review_forwards_additional_context_into_the_actual_system_prompt(monkeypatch):
    verdict = safety_review.SafetyVerdict(passed=True, categories=[], summary="fine")
    calls = _fake_openai(monkeypatch, lambda **kw: _FakeResponse(verdict))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    safety_review.review(
        _make_context(), enabled=True, additional_context="Also flag deal codenames."
    )

    assert "Also flag deal codenames." in calls[0]["input"][0]["content"]


# --- review(): disabled / misconfigured never touch the network -------------------


def test_disabled_never_calls_openai(monkeypatch):
    _raising_openai(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")  # present, but feature is off

    result = safety_review.review(_make_context(), enabled=False)

    assert result is None


def test_missing_api_key_raises_unavailable_without_calling_openai(monkeypatch):
    _raising_openai(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    try:
        safety_review.review(_make_context(), enabled=True)
        raise AssertionError("expected SafetyReviewUnavailable")
    except safety_review.SafetyReviewUnavailable as exc:
        assert "OPENAI_API_KEY" in str(exc)


def test_content_too_large_raises_unavailable_without_calling_openai(monkeypatch):
    _raising_openai(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    huge = safety_review.SafetyReviewContext(
        mapping_key="portmon", files={"big.py": "x" * (safety_review._MAX_REVIEW_CHARS + 1)}
    )

    try:
        safety_review.review(huge, enabled=True)
        raise AssertionError("expected SafetyReviewUnavailable")
    except safety_review.SafetyReviewUnavailable as exc:
        assert "exceeds" in str(exc)


# --- review(): success paths --------------------------------------------------------


def test_review_returns_passed_verdict(monkeypatch):
    verdict = safety_review.SafetyVerdict(passed=True, categories=[], summary="looks generic")
    _fake_openai(monkeypatch, lambda **kw: _FakeResponse(verdict))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    result = safety_review.review(_make_context(), enabled=True)

    assert result is not None  # enabled and the mock returned a verdict, never None here
    assert result == verdict
    assert result.passed is True


def test_review_returns_blocked_verdict_with_categories(monkeypatch):
    verdict = safety_review.SafetyVerdict(
        passed=False, categories=["customer_name"], summary="a customer name appears in a comment"
    )
    _fake_openai(monkeypatch, lambda **kw: _FakeResponse(verdict))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    result = safety_review.review(_make_context(), enabled=True)

    assert result is not None  # enabled and the mock returned a verdict, never None here
    assert result.passed is False
    assert "customer_name" in result.categories


# --- review(): every failure mode is a hard halt, never an implicit pass -----------


def test_generic_exception_raises_unavailable(monkeypatch):
    def behavior(**kw):
        raise RuntimeError("boom")

    _fake_openai(monkeypatch, behavior)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    try:
        safety_review.review(_make_context(), enabled=True)
        raise AssertionError("expected SafetyReviewUnavailable")
    except safety_review.SafetyReviewUnavailable:
        pass


def test_timeout_raises_unavailable(monkeypatch):
    def behavior(**kw):
        raise TimeoutError("request timed out")

    _fake_openai(monkeypatch, behavior)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    try:
        safety_review.review(_make_context(), enabled=True)
        raise AssertionError("expected SafetyReviewUnavailable")
    except safety_review.SafetyReviewUnavailable:
        pass


def test_malformed_output_raises_unavailable(monkeypatch):
    _fake_openai(monkeypatch, lambda **kw: _FakeResponse(None))  # refusal / empty
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    try:
        safety_review.review(_make_context(), enabled=True)
        raise AssertionError("expected SafetyReviewUnavailable")
    except safety_review.SafetyReviewUnavailable:
        pass


# --- cli.py integration: the gate actually halts (or doesn't) the real pipeline ---

def _write_prod_oss_pair(
    tmp_path, *, llm_safety_review_enabled=True, llm_safety_review_additional_context=None
):
    prod = tmp_path / "prod"
    oss = tmp_path / "oss"
    prod.mkdir()
    oss.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    _git(oss, "init", "-q", "-b", "main")

    additional_context_line = (
        f'  additional_context: "{llm_safety_review_additional_context}"\n'
        if llm_safety_review_additional_context
        else ""
    )
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
        # These tests are about safety_review, not pr_writer -- explicitly off so
        # the shared OpenAI mock below (built only to return a SafetyVerdict)
        # isn't also hit by pr_writer now that llm_pr defaults to enabled.
        "llm_pr:\n"
        "  enabled: false\n"
        "llm_safety_review:\n"
        f"  enabled: {str(llm_safety_review_enabled).lower()}\n"
        f"{additional_context_line}",
    )
    base = _commit(prod, "initial")

    _write(oss, "README.md", "# oss\n")
    _commit(oss, "initial")
    return prod, oss, base


def test_disabled_by_default_never_calls_openai_and_sync_succeeds(tmp_path, monkeypatch):
    prod, oss, base = _write_prod_oss_pair(tmp_path, llm_safety_review_enabled=False)
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    _raising_openai(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    exit_code = cli.main(
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


def test_enabled_and_passed_lets_the_pr_open(tmp_path, monkeypatch, capsys):
    prod, oss, base = _write_prod_oss_pair(tmp_path)
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    verdict = safety_review.SafetyVerdict(passed=True, categories=[], summary="fine")
    _fake_openai(monkeypatch, lambda **kw: _FakeResponse(verdict))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    exit_code = cli.main(
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
    assert "dry-run" in capsys.readouterr().out  # got all the way to publish


def test_config_additional_context_reaches_the_real_llm_call(tmp_path, monkeypatch):
    prod, oss, base = _write_prod_oss_pair(
        tmp_path, llm_safety_review_additional_context="Also flag covenant threshold values."
    )
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    verdict = safety_review.SafetyVerdict(passed=True, categories=[], summary="fine")
    calls = _fake_openai(monkeypatch, lambda **kw: _FakeResponse(verdict))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    exit_code = cli.main(["run", "--config", str(prod / "sync" / "monitoring.yaml"),
                           "--source-repo", str(prod), "--dest-repo", str(oss),
                           "--base", base, "--head", head])

    assert exit_code == 0
    assert "Also flag covenant threshold values." in calls[0]["input"][0]["content"]


def test_enabled_and_blocked_halts_with_exit_0_not_a_tool_failure(tmp_path, monkeypatch):
    prod, oss, base = _write_prod_oss_pair(tmp_path)
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    verdict = safety_review.SafetyVerdict(
        passed=False, categories=["customer_name"], summary="a customer name appears in this file"
    )
    _fake_openai(monkeypatch, lambda **kw: _FakeResponse(verdict))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    exit_code = cli.main(
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

    assert exit_code == 0  # correct policy enforcement, not a tool failure
    # Blocked before commit_to_branch ever runs, so no branch was created under this
    # sha prefix at all -- glob, since a real branch (if any existed) would carry a
    # title-derived slug suffix we don't know here.
    branch_prefix = f"sync/portmon/{head[:7]}"
    assert not _git(oss, "branch", "--list", f"{branch_prefix}*").stdout.strip()


def test_enabled_but_unavailable_is_a_real_failure_exit_1(tmp_path, monkeypatch):
    prod, oss, base = _write_prod_oss_pair(tmp_path)
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # enabled but misconfigured

    exit_code = cli.main(
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

    # distinct from the passed=False halt above -- this is broken, not enforcing
    assert exit_code == 1


def test_secret_scan_hit_short_circuits_before_safety_review_even_runs(tmp_path, monkeypatch):
    prod, oss, base = _write_prod_oss_pair(tmp_path)
    _write(prod, "src/portmon/covenant.py", 'AKIA_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    head = _commit(prod, "oops, a real-looking key")

    _raising_openai(monkeypatch)  # would fail the test if constructed at all
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    exit_code = cli.main(
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


def test_block_comment_never_leaks_more_than_the_categorical_summary(tmp_path, monkeypatch, capsys):
    prod, oss, base = _write_prod_oss_pair(tmp_path)
    _write(prod, "src/portmon/covenant.py", "def check():\n    return False\n")
    head = _commit(prod, "change")

    verdict = safety_review.SafetyVerdict(
        passed=False,
        categories=["internal_deal_reference"],
        summary="references an internal deal codename",
    )
    _fake_openai(monkeypatch, lambda **kw: _FakeResponse(verdict))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    cli.main(
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

    printed = capsys.readouterr().out
    assert "internal_deal_reference" in printed
    assert "references an internal deal codename" in printed

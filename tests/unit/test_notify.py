import subprocess

from sync_service.lib import notify


def test_comment_on_commit_prints_and_also_posts_to_slack(monkeypatch, capsys):
    # GITHUB_REPOSITORY is set automatically on every real GitHub Actions runner --
    # without neutralizing it, this test's outcome depends on whether it happens to
    # run locally (unset) or in real CI (always set), rather than being deterministic.
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    fake_sha = "abc123def456789"  # pragma: allowlist secret
    result = notify.comment_on_commit(fake_sha, "something halted")

    assert "abc123def456"[:12] in result  # pragma: allowlist secret
    assert "something halted" in result
    assert captured == [result]  # the same message, not a re-derived one
    assert result in capsys.readouterr().out


def test_comment_on_commit_fences_the_body_as_a_code_block(monkeypatch):
    # Renders as a code block wherever it's posted -- print, Slack, and the
    # real GitHub comment all get the same fenced body, built once.
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    result = notify.comment_on_commit("abc123", "nothing to commit, no PR.")

    assert "```\nnothing to commit, no PR.\n```" in result
    assert captured[0] == result  # Slack gets the exact same fenced body


def test_comment_on_commit_still_returns_normally_if_slack_post_fails(monkeypatch):
    monkeypatch.setattr(notify.slack, "post", lambda text: False)

    result = notify.comment_on_commit("abc123", "body text")

    assert "body text" in result  # slack failing never raises or changes the return


def test_comment_on_commit_links_the_sha_when_github_repository_is_set(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "chinh100x/prod")
    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    result = notify.comment_on_commit(
        "e3369bc4f775216af1ccc45d2c1aa8098d5164b0",  # pragma: allowlist secret
        "something halted",
    )

    url = "https://github.com/chinh100x/prod/commit/e3369bc4f775216af1ccc45d2c1aa8098d5164b0"
    # Printed/returned: a bare URL -- GitHub Actions' own log viewer auto-linkifies
    # this, but would show mrkdwn's <...|...> brackets as literal text instead.
    assert url in result
    assert "<" not in result
    printed = capsys.readouterr().out
    assert url in printed
    # Slack: the same URL, but as a short, readable link via its own <url|text>
    # mrkdwn syntax rather than a full URL sitting in the message body.
    assert f"<{url}|e3369bc4f775>" in captured[0]


def test_comment_on_commit_falls_back_to_a_bare_sha_without_github_repository(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(notify.slack, "post", lambda text: True)

    fake_sha = "abc123def456789"  # pragma: allowlist secret
    result = notify.comment_on_commit(fake_sha, "something halted")

    assert "abc123def456" in result  # pragma: allowlist secret
    assert "https://" not in result  # nothing to link to locally/in tests


# --- comment_on_commit(): the real GitHub API call, when a token is available ---


def test_comment_on_commit_posts_a_real_github_comment_when_token_and_repo_are_set(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_REPOSITORY", "chinh100x/prod")
    monkeypatch.setattr(notify.slack, "post", lambda text: True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(notify.subprocess, "run", fake_run)

    fake_sha = "abc123def456"  # pragma: allowlist secret
    fake_token = "ghp_faketoken"  # pragma: allowlist secret
    notify.comment_on_commit(fake_sha, "something halted", token=fake_token)

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == [
        "gh",
        "api",
        "repos/chinh100x/prod/commits/abc123def456/comments",
        "-f",
        "body=```\nsomething halted\n```",
    ]
    assert kwargs["env"]["GH_TOKEN"] == "ghp_faketoken"


def test_comment_on_commit_skips_the_real_api_call_without_a_token(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "chinh100x/prod")
    monkeypatch.setattr(notify.slack, "post", lambda text: True)
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    notify.comment_on_commit("abc123", "body text")  # token defaults to None

    assert calls == []


def test_comment_on_commit_skips_the_real_api_call_without_a_repo(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(notify.slack, "post", lambda text: True)
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    notify.comment_on_commit("abc123", "body text", token="ghp_faketoken")

    assert calls == []  # nothing to address the API call to, locally


def test_comment_on_commit_real_api_failure_never_raises(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "chinh100x/prod")
    monkeypatch.setattr(notify.slack, "post", lambda text: True)
    monkeypatch.setattr(
        notify.subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="422 no scope"),
    )

    result = notify.comment_on_commit("abc123", "body text", token="ghp_faketoken")

    assert "body text" in result  # the print + Slack channel already succeeded regardless
    assert "GitHub commit comment failed" in capsys.readouterr().out


def test_pr_opened_posts_a_slack_link_using_the_title_as_link_text(monkeypatch, capsys):
    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    notify.pr_opened("Prod", "Improve covenant validation", "https://github.com/x/y/pull/1")

    assert len(captured) == 1
    assert captured[0].startswith("[Prod] PR opened:")
    # Slack's own mrkdwn link syntax is <url|text> -- not "[text](url)", which
    # Slack does not render as a link at all.
    assert "<https://github.com/x/y/pull/1|Improve covenant validation>" in captured[0]
    assert capsys.readouterr().out == ""  # unlike comment_on_commit, nothing printed


def test_pr_opened_falls_back_to_mechanical_label_when_project_name_unset(monkeypatch):
    # sync.py computes this fallback (label:mapping_key) itself and passes it in as
    # project_label when SyncConfig.project_name isn't set -- notify.py just prints
    # whatever project_label it's given, it doesn't know about the fallback rule.
    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    notify.pr_opened("sync:portmon", "Sync portmon changes", "https://github.com/x/y/pull/1")

    assert captured[0].startswith("[sync:portmon] PR opened:")


def test_pr_opened_dry_run_posts_the_title_with_no_fake_link(monkeypatch):
    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    notify.pr_opened(
        "Prod",
        "Sync portmon changes",
        "[dry-run: no remote configured] Would open PR sync/portmon/abc123 -> main\nTitle: ...",
    )

    assert len(captured) == 1
    assert "Sync portmon changes" in captured[0]
    assert "dry-run" in captured[0]
    assert "<" not in captured[0]  # no Slack link syntax around dry-run preview text

from sync_service import notify


def test_comment_on_commit_prints_and_also_posts_to_slack(monkeypatch, capsys):
    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    result = notify.comment_on_commit("abc123def456789", "something halted")

    assert "abc123def456"[:12] in result
    assert "something halted" in result
    assert captured == [result]  # the same message, not a re-derived one
    assert result in capsys.readouterr().out


def test_comment_on_commit_still_returns_normally_if_slack_post_fails(monkeypatch):
    monkeypatch.setattr(notify.slack, "post", lambda text: False)

    result = notify.comment_on_commit("abc123", "body text")

    assert "body text" in result  # slack failing never raises or changes the return


def test_pr_opened_posts_to_slack_only_no_github_comment_print(monkeypatch, capsys):
    captured = []
    monkeypatch.setattr(notify.slack, "post", lambda text: captured.append(text) or True)

    notify.pr_opened("sync", "portmon", "https://github.com/x/y/pull/1")

    assert len(captured) == 1
    assert "portmon" in captured[0]
    assert "https://github.com/x/y/pull/1" in captured[0]
    assert capsys.readouterr().out == ""  # unlike comment_on_commit, nothing printed

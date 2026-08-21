from sync_service.lib.config import RedactRule
from sync_service.lib.scrub import redact_text


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


def test_redact_text_leaves_content_unchanged_when_nothing_matches():
    rule = RedactRule(pattern=r"NEVER_PRESENT_TOKEN", replace="<X>")

    text, categories = redact_text("no match here\n", [rule])

    assert text == "no match here\n"
    assert categories == []

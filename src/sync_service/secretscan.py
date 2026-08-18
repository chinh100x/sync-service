"""Secret scan: a hard gate over the scrubbed content. A hit stops the run;
no PR, no auto-redaction attempt.

Small built-in scanner for local development only. Production deployment should
run `gitleaks` over the same desired-tree contents instead — see README.md.
"""

from __future__ import annotations

import re

_PATTERNS = {
    "aws_access_key_id": re.compile(r"AKIA[0-9A-Z]{16}"),
    "generic_api_key": re.compile(r"(?i)api[_-]?key['\"]?\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
    "private_key_header": re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "generic_secret_assignment": re.compile(
        r"(?i)(secret|token|password)['\"]?\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"
    ),
}


class SecretHit(dict):
    """{"path": ..., "rule": ...} -- deliberately no matched value, since this gets
    printed/logged and the actual secret text must never enter it in the first place."""


def scan(desired: dict[str, str]) -> list[SecretHit]:
    hits: list[SecretHit] = []
    for path, text in desired.items():
        for rule_name, pattern in _PATTERNS.items():
            if pattern.search(text):
                hits.append(SecretHit(path=path, rule=rule_name))
    return hits

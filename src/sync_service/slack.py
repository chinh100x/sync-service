"""Slack notifications — a second, best-effort channel alongside notify.py's GitHub
commit comments. Posts to an Incoming Webhook URL configured via SLACK_WEBHOOK_URL.

Best-effort by design: Slack availability must never become a dependency of the
sync succeeding, so every failure (no webhook configured, network error, a non-2xx
response) is swallowed and logged, never raised. Contrast with safety_review.py,
where a failure to reach OpenAI is a hard halt -- this module has no authority
over the sync at all, only pr_writer.py-style "best effort, never a dependency."
"""
from __future__ import annotations

import json
import os
import urllib.request

_TIMEOUT_SECONDS = 10.0


def post(text: str) -> bool:
    """Returns True if the message was actually delivered, False for every other
    reason (not configured, network error, non-2xx response) -- never raises."""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return False
    try:
        body = json.dumps({"text": text}).encode("utf-8")
        request = urllib.request.Request(
            webhook_url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        print(f"[slack] notification failed ({type(exc).__name__}), continuing anyway")
        return False

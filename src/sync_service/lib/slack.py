"""Slack notifications -- a second, best-effort channel alongside notify.py's GitHub
commit comments. Posts to an Incoming Webhook URL (SLACK_WEBHOOK_URL), optionally
directed at a channel (SLACK_CHANNEL, ignored by webhook apps that don't support it).

Best-effort by design: every failure is swallowed and logged, never raised --
Slack availability must never become a dependency of the sync succeeding.
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
    payload = {"text": text}
    channel = os.environ.get("SLACK_CHANNEL")
    if channel:
        payload["channel"] = channel
    try:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            webhook_url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            delivered = 200 <= response.status < 300
            print(f"[slack] {'posted' if delivered else f'failed (status {response.status})'}")
            return delivered
    except Exception as exc:
        print(f"[slack] notification failed ({type(exc).__name__}), continuing anyway")
        return False

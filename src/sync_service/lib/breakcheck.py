"""Break check: "still works" checked before the PR exists.

install/run come from the mapping config, but they execute against the far side's
tracked content — which, for OSS -> production or a public repo's own contributions,
is content this service didn't originate. It should never run with a write-capable
token in its environment.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import BreakCheck

_STRIP_ENV_VARS = {"GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "SLACK_WEBHOOK_URL"}


@dataclass
class CheckResult:
    passed: bool
    failed_step: str | None = None
    output: str = ""


def run(work_dir: Path, check: BreakCheck) -> CheckResult:
    sanitized_env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_VARS}

    for step_name, command in (("install", check.install), ("run", check.run)):
        proc = subprocess.run(
            shlex.split(command),
            cwd=work_dir,
            capture_output=True,
            text=True,
            env=sanitized_env,
        )
        if proc.returncode != 0:
            return CheckResult(
                passed=False,
                failed_step=step_name,
                output=(proc.stdout + proc.stderr).strip(),
            )
    return CheckResult(passed=True)

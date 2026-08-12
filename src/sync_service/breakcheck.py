"""Break check — design.md §3. "Still works" checked before the PR exists."""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import BreakCheck


@dataclass
class CheckResult:
    passed: bool
    failed_step: str | None = None
    output: str = ""


def run(work_dir: Path, check: BreakCheck) -> CheckResult:
    for step_name, command in (("install", check.install), ("run", check.run)):
        proc = subprocess.run(
            shlex.split(command),
            cwd=work_dir,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return CheckResult(
                passed=False,
                failed_step=step_name,
                output=(proc.stdout + proc.stderr).strip(),
            )
    return CheckResult(passed=True)

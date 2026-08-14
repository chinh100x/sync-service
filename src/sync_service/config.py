"""Mapping config schema.

Loads and validates a repo pair's sync/*.yaml. Fails fast on load
(bad regex, overlapping dest paths) rather than mid-run.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, field_validator, model_validator


class RedactRule(BaseModel):
    pattern: str
    replace: str
    # Category name only, never the matched value -- surfaced to pr_writer.py as
    # scrubbed_categories so the PR body can say what kind of thing was excluded.
    category: str | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        re.compile(v)  # raises at load time if invalid
        return v

    @property
    def compiled(self) -> re.Pattern:
        return re.compile(self.pattern)


class BreakCheck(BaseModel):
    install: str
    run: str


class Mapping(BaseModel):
    key: str
    source: str = "."  # whole repo by default; narrow to a subdirectory if needed
    dest: str = "."
    exclude: list[str] = []
    redact: list[RedactRule] = []
    break_check: BreakCheck
    # Static, human-authored "why this mapping propagates" -- the one place free text
    # can appear in the PR body without coming from the (untrusted) production commit.
    public_reason: str | None = None

    @field_validator("dest")
    @classmethod
    def _relative(cls, v: str) -> str:
        if v.startswith("/") or ".." in Path(v).parts:
            raise ValueError(f"dest must be a relative, non-escaping path: {v}")
        return v


class LLMPRConfig(BaseModel):
    # Off by default -- pr_writer.py falls back to a deterministic writer on any
    # failure/missing key, so OpenAI is never a dependency of the sync itself.
    enabled: bool = False


class SafetyReviewConfig(BaseModel):
    # Off by default. Unlike llm_pr, a failure here (missing key, API error, content
    # too large) is a hard halt, not a safe fallback -- see safety_review.py.
    enabled: bool = False


class SyncConfig(BaseModel):
    mappings: list[Mapping]
    llm_pr: LLMPRConfig = LLMPRConfig()
    llm_safety_review: SafetyReviewConfig = SafetyReviewConfig()
    # Human-readable label for Slack notifications (e.g. "Prod"); unset falls back
    # to the mechanical `label:mapping_key` prefix (see cli.py's run_direction).
    project_name: str | None = None

    @model_validator(mode="after")
    def _non_overlapping_dest(self) -> Self:
        dests = [m.dest.rstrip("/") for m in self.mappings]
        for i, a in enumerate(dests):
            for b in dests[i + 1 :]:
                if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                    raise ValueError(f"overlapping dest paths in mapping config: {a!r} vs {b!r}")
        return self

    @classmethod
    def load(cls, path: str | Path) -> "SyncConfig":
        raw = yaml.safe_load(Path(path).read_text())
        return cls.model_validate(raw)

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
    # Optional label for what kind of thing this rule strips (e.g. "tenant_config",
    # "internal_endpoint") -- never the matched value itself, just a category name.
    # Surfaced to the LLM PR writer (pr_writer.py) as scrubbed_categories so a PR body
    # can truthfully say "production-specific configuration was excluded" without
    # ever naming what it was.
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
    # Reverse of `redact` — applied when a change flows dest -> source (OSS -> production),
    # e.g. placeholder -> real endpoint. Only relevant if the OSS -> production direction is enabled.
    hydrate: list[RedactRule] = []
    break_check: BreakCheck
    # Break check to run in the *production* repo when propagating dest -> source.
    # No default command makes sense here (production repos rarely expose one uniform
    # "run" command the way OSS demo/smoke commands do) — omit to skip and rely on the
    # production repo's own CI as the safety net for that direction.
    reverse_break_check: BreakCheck | None = None
    # A human-authored, static line explaining why this mapping propagates at all --
    # written once for public consumption, same as `key`/`source`/`dest` already are.
    # Not derived from any commit: the PR body needs *some* safe "why" (see v7's note
    # on why the production commit message can't be that), and this is the one place
    # free text can appear without it coming from an uncontrolled source.
    public_reason: str | None = None

    @field_validator("dest")
    @classmethod
    def _relative(cls, v: str) -> str:
        if v.startswith("/") or ".." in Path(v).parts:
            raise ValueError(f"dest must be a relative, non-escaping path: {v}")
        return v


class LLMPRConfig(BaseModel):
    # Off by default: the PR title/body stay fully deterministic (pr_writer.py's
    # DeterministicPRWriter) unless a mapping config explicitly opts in. Enabling this
    # never makes OpenAI availability a dependency of the sync itself -- pr_writer.py
    # falls back to the deterministic writer on any failure, missing key, or when
    # this is False.
    enabled: bool = False


class SafetyReviewConfig(BaseModel):
    # Off by default. Unlike llm_pr, enabling this and then having it fail to run
    # (missing key, API error, content too large) is NOT a safe fallback -- it's a
    # hard halt, no PR. See safety_review.py's module docstring.
    enabled: bool = False


class SyncConfig(BaseModel):
    mappings: list[Mapping]
    llm_pr: LLMPRConfig = LLMPRConfig()
    llm_safety_review: SafetyReviewConfig = SafetyReviewConfig()
    # Human-readable label for this deployment (e.g. "Prod"), used only in
    # Slack-bound notifications -- swaps the mechanical `[sync:app]`-style prefix
    # for something a channel of humans can actually read at a glance. Optional:
    # unset means each notification falls back to the old mechanical
    # `label:mapping_key` prefix (see cli.py's run_direction).
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

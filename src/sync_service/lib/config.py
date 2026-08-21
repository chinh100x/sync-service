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
    category: str | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        re.compile(v)
        return v

    @property
    def compiled(self) -> re.Pattern:
        return re.compile(self.pattern)


class BreakCheck(BaseModel):
    install: str
    run: str


class Mapping(BaseModel):
    key: str
    source: str = "."
    dest: str = "."
    exclude: list[str] = []
    redact: list[RedactRule] = []
    break_check: BreakCheck
    public_reason: str | None = None

    @field_validator("dest")
    @classmethod
    def _relative(cls, v: str) -> str:
        if v.startswith("/") or ".." in Path(v).parts:
            raise ValueError(f"dest must be a relative, non-escaping path: {v}")
        return v


class LLMPRConfig(BaseModel):
    enabled: bool = True


class SafetyReviewConfig(BaseModel):
    enabled: bool = True
    additional_context: str | None = None


class SyncConfig(BaseModel):
    mappings: list[Mapping]
    llm_pr: LLMPRConfig = LLMPRConfig()
    llm_safety_review: SafetyReviewConfig = SafetyReviewConfig()
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
    def load(cls, path: str | Path) -> SyncConfig:
        raw = yaml.safe_load(Path(path).read_text())
        return cls.model_validate(raw)

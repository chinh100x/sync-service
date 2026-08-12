"""Mapping config schema — architecture.md §3.

Loads and validates a repo pair's sync/*.yaml. Fails fast on load
(bad regex, overlapping dest paths) rather than mid-run.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, field_validator, model_validator


class Destination(BaseModel):
    repo: str
    branch: str = "main"


class RedactRule(BaseModel):
    pattern: str
    replace: str

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
    source: str
    dest: str
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

    @field_validator("dest")
    @classmethod
    def _relative(cls, v: str) -> str:
        if v.startswith("/") or ".." in Path(v).parts:
            raise ValueError(f"dest must be a relative, non-escaping path: {v}")
        return v


class SyncConfig(BaseModel):
    destination: Destination
    mappings: list[Mapping]

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

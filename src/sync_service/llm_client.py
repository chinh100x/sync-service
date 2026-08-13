"""Shared OpenAI structured-output call, used by both pr_writer.py and
safety_review.py. Only the plumbing is shared here -- constructing the client and
making one `responses.parse(..., store=False)` call. What a failure *means* stays
entirely at the call site: pr_writer.py treats any failure as "fall back to the
deterministic writer" (cosmetic, fail-open); safety_review.py treats any failure
as "halt, no PR" (a security gate, fail-closed). Deliberately not decided here.
"""
from __future__ import annotations

from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel

DEFAULT_MODEL = "gpt-5.6"
DEFAULT_TIMEOUT_SECONDS = 20.0

_T = TypeVar("_T", bound=BaseModel)


class LLMCallFailed(Exception):
    """Raised only when the call succeeded but produced no usable structured
    output (a refusal or an empty response). Any other failure -- timeout, auth,
    rate limit, network error -- propagates as whatever the openai SDK itself
    raises; callers decide how to handle both kinds, this function doesn't."""


def structured_call(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: str,
    text_format: type[_T],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> _T:
    client = OpenAI(api_key=api_key, timeout=timeout)
    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        text_format=text_format,
        store=False,  # minimize server-side retention of whatever this call sent
    )
    parsed = response.output_parsed
    if parsed is None:
        raise LLMCallFailed("model returned no usable structured output (refusal or empty response)")
    return parsed

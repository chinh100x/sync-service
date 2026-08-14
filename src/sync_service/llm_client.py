"""Shared OpenAI structured-output call, used by pr_writer.py and safety_review.py.
Only the plumbing is shared -- what a failure *means* (fail-open vs. fail-closed)
is decided entirely at the call site, not here.
"""
from __future__ import annotations

from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel

DEFAULT_MODEL = "gpt-5.6"
DEFAULT_TIMEOUT_SECONDS = 20.0

_T = TypeVar("_T", bound=BaseModel)


class LLMCallFailed(Exception):
    """Only for a call that succeeded but returned no usable output (refusal or
    empty response). Other failures propagate as whatever the openai SDK raises."""


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

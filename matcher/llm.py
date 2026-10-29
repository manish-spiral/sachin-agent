"""Structured-output LLM call for the matcher (OpenAI or Groq, see shared/llm.py)."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from shared.llm import LLMConfig, make_client
from shared.schemas.failure import FailureClass

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    def __init__(self, message: str, *, failure_class: FailureClass) -> None:
        super().__init__(message)
        self.failure_class = failure_class


def parse_structured(system: str, user: str, model_cls: type[T], *, config: LLMConfig) -> T:
    import openai

    client = make_client(config)
    try:
        response = client.responses.parse(
            model=config.model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            text_format=model_cls,
            **config.request_options(),
        )
    except openai.RateLimitError as exc:
        raise LLMError(str(exc), failure_class=FailureClass.RATE_LIMIT) from exc
    except openai.APIError as exc:
        raise LLMError(str(exc), failure_class=FailureClass.TRANSIENT) from exc

    parsed = response.output_parsed
    if parsed is None:
        raise LLMError(
            f"no structured output (refusal: {getattr(response, 'refusal', None)})",
            failure_class=FailureClass.SEMANTIC_MISMATCH,
        )
    return parsed

"""Structured-output LLM call for the matcher. OpenAI by default."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from shared.config import get_settings
from shared.schemas.failure import FailureClass

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    def __init__(self, message: str, *, failure_class: FailureClass) -> None:
        super().__init__(message)
        self.failure_class = failure_class


def parse_structured(system: str, user: str, model_cls: type[T], *, model: str) -> T:
    settings = get_settings()
    if not settings.openai_api_key:
        raise LLMError("OPENAI_API_KEY is not set. Add it to .env.", failure_class=FailureClass.AUTH_WALL)

    import openai

    client = openai.OpenAI(api_key=settings.openai_api_key)
    try:
        response = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            text_format=model_cls,
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

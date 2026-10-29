"""Structured-output LLM call for the matcher (OpenAI or Groq, see shared/llm.py)."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from shared.llm import LLMConfig, Task, make_client, resolve
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
    except openai.BadRequestError as exc:
        # json_validate_failed: the model produced a bad draft (truncated or
        # off-schema). Retryable, unlike a quota or auth error.
        if exc.code == "json_validate_failed":
            raise LLMError(str(exc), failure_class=FailureClass.SEMANTIC_MISMATCH) from exc
        raise LLMError(str(exc), failure_class=FailureClass.TRANSIENT) from exc
    except openai.AuthenticationError as exc:
        raise LLMError(str(exc), failure_class=FailureClass.AUTH_WALL) from exc
    except openai.APIError as exc:
        raise LLMError(str(exc), failure_class=FailureClass.TRANSIENT) from exc

    parsed = response.output_parsed
    if parsed is None:
        raise LLMError(
            f"no structured output (refusal: {getattr(response, 'refusal', None)})",
            failure_class=FailureClass.SEMANTIC_MISMATCH,
        )
    return parsed


class StructuredLLM:
    """`llm(task, system, user, Model)` → Model, with one resolved config per task.

    Tailoring makes several kinds of call per job; tests replace this whole
    object with a fake, so nothing here needs mocking.
    """

    def __init__(self) -> None:
        self._configs: dict[Task, LLMConfig] = {}

    def config(self, task: Task) -> LLMConfig:
        if task not in self._configs:
            self._configs[task] = resolve(task)
        return self._configs[task]

    def model_name(self, task: Task) -> str:
        config = self.config(task)
        return f"{config.provider.value}:{config.model}"

    def __call__(self, task: Task, system: str, user: str, model_cls: type[T]) -> T:
        return parse_structured(system, user, model_cls, config=self.config(task))

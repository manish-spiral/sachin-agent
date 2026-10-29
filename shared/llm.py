"""LLM provider selection. Infrastructure only: which endpoint, key and model.

Groq exposes an OpenAI-compatible API, so both providers use the `openai` SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from shared.config import get_settings

if TYPE_CHECKING:
    import openai


class Provider(StrEnum):
    OPENAI = "openai"
    GROQ = "groq"


class Task(StrEnum):
    EXTRACTION = "extraction"
    SCORING = "scoring"
    KEYWORDS = "keywords"
    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    VERIFY = "verify"


_BASE_URLS = {Provider.OPENAI: None, Provider.GROQ: "https://api.groq.com/openai/v1"}
_DEFAULT_MODELS = {
    (Provider.OPENAI, Task.EXTRACTION): "gpt-4o-2024-08-06",
    (Provider.OPENAI, Task.SCORING): "gpt-4o-mini",
    (Provider.OPENAI, Task.KEYWORDS): "gpt-4o-mini",
    (Provider.OPENAI, Task.RESUME): "gpt-4o-2024-08-06",
    (Provider.OPENAI, Task.COVER_LETTER): "gpt-4o-2024-08-06",
    (Provider.OPENAI, Task.VERIFY): "gpt-4o-2024-08-06",
    (Provider.GROQ, Task.EXTRACTION): "openai/gpt-oss-120b",
    (Provider.GROQ, Task.SCORING): "openai/gpt-oss-120b",
    # Groq quotas are per model: a short extraction task on the small model
    # leaves the large model's daily tokens for writing and verifying.
    (Provider.GROQ, Task.KEYWORDS): "openai/gpt-oss-20b",
    (Provider.GROQ, Task.RESUME): "openai/gpt-oss-120b",
    (Provider.GROQ, Task.COVER_LETTER): "openai/gpt-oss-120b",
    # A checker from a different model family makes different mistakes than
    # the writer it checks, and draws on a separate quota.
    (Provider.GROQ, Task.VERIFY): "qwen/qwen3.8-27b",
}
# gpt-oss models reason before answering. Extraction is a one-off where a
# dropped section costs real data, so it reasons more; scoring runs per job and
# is bounded by Groq's per-minute token limit. OpenAI's defaults don't reason.
_REASONING_EFFORT = {
    (Provider.GROQ, Task.EXTRACTION): "medium",
    (Provider.GROQ, Task.SCORING): "low",
    (Provider.GROQ, Task.KEYWORDS): "low",
    (Provider.GROQ, Task.RESUME): "medium",
    (Provider.GROQ, Task.COVER_LETTER): "medium",
}
# Reasoning tokens count against the output budget. Groq's default left too
# little room for reasoning plus a full resume JSON (seen live: truncated JSON
# missing "roles"), so writing tasks get an explicit budget.
_MAX_OUTPUT_TOKENS = {Task.RESUME: 8000, Task.COVER_LETTER: 6000, Task.VERIFY: 4000}
# Groq's free tier allows ~8k tokens/minute; the SDK honours Retry-After, so
# extra retries turn a 429 into a short wait instead of a failed job.
_MAX_RETRIES = {Provider.OPENAI: 2, Provider.GROQ: 8}


class LLMNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    provider: Provider
    # Kept out of repr so a logged or printed config never leaks the key.
    api_key: str = field(repr=False)
    model: str
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None

    def request_options(self) -> dict:
        """Extra kwargs for `responses.parse`, beyond model/input/text_format."""
        options: dict = {}
        if self.reasoning_effort:
            options["reasoning"] = {"effort": self.reasoning_effort}
        if self.max_output_tokens:
            options["max_output_tokens"] = self.max_output_tokens
        return options


def resolve(task: Task) -> LLMConfig:
    """Explicit LLM_PROVIDER wins; otherwise use whichever key is set (OpenAI first)."""
    s = get_settings()
    keys = {Provider.OPENAI: s.openai_api_key, Provider.GROQ: s.groq_api_key}
    if s.llm_provider:
        provider = Provider(s.llm_provider)
    else:
        provider = next((p for p in Provider if keys[p]), Provider.OPENAI)
    api_key = keys[provider]
    if not api_key:
        raise LLMNotConfigured(
            f"No API key for LLM provider {provider.value!r}. "
            "Set OPENAI_API_KEY or GROQ_API_KEY in .env."
        )
    named = {Task.EXTRACTION: s.extraction_model, Task.SCORING: s.scoring_model}
    override = s.llm_models.get(task.value) or named.get(task)
    return LLMConfig(
        provider=provider,
        api_key=api_key,
        model=override or _DEFAULT_MODELS[provider, task],
        # A custom model may not accept a reasoning parameter; only defaults get one.
        reasoning_effort=None if override else _REASONING_EFFORT.get((provider, task)),
        max_output_tokens=_MAX_OUTPUT_TOKENS.get(task),
    )


def make_client(config: LLMConfig) -> openai.OpenAI:
    import openai

    return openai.OpenAI(
        api_key=config.api_key,
        base_url=_BASE_URLS[config.provider],
        max_retries=_MAX_RETRIES[config.provider],
    )

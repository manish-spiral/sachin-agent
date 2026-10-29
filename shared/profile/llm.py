"""LLM-backed profile extraction (OpenAI or Groq, see shared/llm.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from shared.llm import LLMConfig, LLMNotConfigured, Task, make_client, resolve
from shared.profile.faithfulness import faithfulness_issues
from shared.profile.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_TEMPLATE,
    PROMPT_VERSION,
)
from shared.schemas.profile import Profile

if TYPE_CHECKING:
    import openai


class ExtractionError(RuntimeError):
    pass


# Extraction is a one-off per CV, so a couple of retries are cheap insurance
# against a run that drops a section (seen live with gpt-oss on Groq).
MAX_ATTEMPTS = 3


def _extract_once(client: openai.OpenAI, config: LLMConfig, cv_text: str) -> Profile:
    response = client.responses.parse(
        model=config.model,
        input=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": EXTRACTION_USER_TEMPLATE.format(cv_text=cv_text)},
        ],
        text_format=Profile,
        **config.request_options(),
    )
    parsed = response.output_parsed
    if parsed is None:
        raise ExtractionError(
            "LLM refused or returned no structured output. "
            f"Refusal: {getattr(response, 'refusal', None)}"
        )
    return parsed


def extract_profile_from_text(cv_text: str) -> Profile:
    """Extract a draft, retrying while it fails the faithfulness check; keeps the
    draft with the fewest issues. The caller should still show remaining issues."""
    try:
        config = resolve(Task.EXTRACTION)
    except LLMNotConfigured as exc:
        raise ExtractionError(
            f"{exc} Or write a profile by hand with `sachin-agent-profile init`."
        ) from exc

    client = make_client(config)
    logger.info(
        f"profile extraction: prompt={PROMPT_VERSION} model={config.provider}:{config.model} "
        f"chars={len(cv_text)}"
    )

    best: tuple[Profile, list[str]] | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        profile = _extract_once(client, config, cv_text)
        issues = faithfulness_issues(profile, cv_text)
        if best is None or len(issues) < len(best[1]):
            best = (profile, issues)
        if not issues:
            break
        logger.warning(f"extraction attempt {attempt}: {len(issues)} faithfulness issue(s)")

    assert best is not None
    profile = best[0]
    # Defensive: force finalized=false regardless of what the model returned.
    profile.finalized = False
    return profile


# ---------------------------------------------------------------------------
# Anthropic swap (uncomment to use Claude instead of OpenAI)
# ---------------------------------------------------------------------------
# def extract_profile_from_text(cv_text: str) -> Profile:
#     import anthropic
#     settings = get_settings()
#     if not settings.anthropic_api_key:
#         raise ExtractionError("ANTHROPIC_API_KEY not set")
#     client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
#     schema: dict[str, Any] = Profile.model_json_schema()
#     response = client.messages.create(
#         model="claude-sonnet-4-20250514",
#         max_tokens=4096,
#         system=EXTRACTION_SYSTEM_PROMPT,
#         tools=[{
#             "name": "store_profile",
#             "description": "Store the extracted profile.",
#             "input_schema": schema,
#         }],
#         tool_choice={"type": "tool", "name": "store_profile"},
#         messages=[{"role": "user",
#                    "content": EXTRACTION_USER_TEMPLATE.format(cv_text=cv_text)}],
#     )
#     tool_block = next(b for b in response.content if b.type == "tool_use")
#     profile = Profile.model_validate(tool_block.input)
#     profile.finalized = False
#     return profile
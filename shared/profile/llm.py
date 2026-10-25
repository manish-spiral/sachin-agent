"""LLM-backed profile extraction. OpenAI by default."""

from __future__ import annotations

from loguru import logger

from shared.config import get_settings
from shared.profile.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_TEMPLATE,
    PROMPT_VERSION,
)
from shared.schemas.profile import Profile


class ExtractionError(RuntimeError):
    pass


def extract_profile_from_text(cv_text: str) -> Profile:
    settings = get_settings()
    if not settings.openai_api_key:
        raise ExtractionError(
            "OPENAI_API_KEY is not set. Add it to .env, or paste a profile "
            "manually with `sachin-agent-profile init`."
        )

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)

    logger.info(f"profile extraction: prompt={PROMPT_VERSION} chars={len(cv_text)}")

    response = client.responses.parse(
        model="gpt-4o-2024-08-06",
        input=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": EXTRACTION_USER_TEMPLATE.format(cv_text=cv_text)},
        ],
        text_format=Profile,
    )

    parsed = response.output_parsed
    if parsed is None:
        raise ExtractionError(
            "LLM refused or returned no structured output. "
            f"Refusal: {getattr(response, 'refusal', None)}"
        )

    # Defensive: force finalized=false regardless of what the model returned.
    parsed.finalized = False
    return parsed


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
"""Structured reading of a job description, cached on Job.requirements."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from matcher.llm import StructuredLLM
from matcher.prompts import KEYWORDS_PROMPT_VERSION, KEYWORDS_SYSTEM_PROMPT, KEYWORDS_USER_TEMPLATE
from shared.db.engine import get_session
from shared.llm import Task
from shared.schemas.job import Job

DESCRIPTION_CHARS = 8000


class JobRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    must_have: list[str]
    nice_to_have: list[str]
    keywords: list[str]
    responsibilities: list[str]


def get_requirements(job: Job, llm: StructuredLLM) -> JobRequirements:
    """Cached per job and prompt version; extracting costs one small-model call."""
    cached = job.requirements or {}
    if cached.get("prompt_version") == KEYWORDS_PROMPT_VERSION:
        return JobRequirements.model_validate(cached["requirements"])

    requirements = llm(
        Task.KEYWORDS,
        KEYWORDS_SYSTEM_PROMPT,
        KEYWORDS_USER_TEMPLATE.format(
            title=job.title, company=job.company, description=job.description_raw[:DESCRIPTION_CHARS]
        ),
        JobRequirements,
    )
    with get_session() as session:
        row = session.get(Job, job.id)
        if row is not None:
            row.requirements = {
                "prompt_version": KEYWORDS_PROMPT_VERSION,
                "requirements": requirements.model_dump(),
            }
            session.add(row)
            session.commit()
    return requirements

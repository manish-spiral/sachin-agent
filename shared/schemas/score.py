"""Fit-score contract. The matcher writes it into Job.score_breakdown; review,
the applier and the metrics dashboard read it. Frozen: additive changes only."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

DimensionValue = Literal[1, 2, 3, 4, 5]


class Dimension(StrEnum):
    ROLE_MATCH = "role_match"
    SKILLS_OVERLAP = "skills_overlap"
    SENIORITY = "seniority"
    COMPENSATION = "compensation"
    LOCATION = "location"
    VISA = "visa"


class DimensionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: DimensionValue
    reason: str


class ScoreBreakdown(BaseModel):
    """What the LLM returns. One 1–5 score + reason per rubric dimension."""

    model_config = ConfigDict(extra="forbid")
    role_match: DimensionScore
    skills_overlap: DimensionScore
    seniority: DimensionScore
    compensation: DimensionScore
    location: DimensionScore
    visa: DimensionScore
    summary: str


class ScoreMethod(StrEnum):
    LLM = "llm"
    PREFILTER = "prefilter"


class StoredScore(BaseModel):
    """What lands in Job.score_breakdown, with enough provenance to correlate
    a score with the prompt, model, weights and profile that produced it."""

    model_config = ConfigDict(extra="forbid")
    method: ScoreMethod
    overall: float
    breakdown: ScoreBreakdown | None = None
    prefilter_reason: str | None = None
    weights: dict[str, float] = {}
    prompt_version: str | None = None
    model: str | None = None
    profile_version: int | None = None

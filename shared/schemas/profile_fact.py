"""Atomic, citable applicant facts derived from the Profile.

Every LLM that writes about the applicant (scoring reasons, resume bullets,
form answers) is shown these facts by id and must cite the ids it relied on.
Code then checks each citation against this table, so a claim with no fact
behind it is caught instead of trusted. Facts are never deleted: a fact
removed from the profile goes inactive, so old citations still resolve.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field, SQLModel


class FactKind(StrEnum):
    IDENTITY = "identity"
    WORK_AUTHORIZATION = "work_authorization"
    PREFERENCE = "preference"
    EXPERIENCE = "experience"
    EXPERIENCE_BULLET = "experience_bullet"
    TECHNOLOGY = "technology"
    EDUCATION = "education"
    SKILL = "skill"
    PROJECT = "project"
    CERTIFICATION = "certification"
    ANSWER = "answer"


def _now() -> datetime:
    return datetime.now(UTC)


class ProfileFact(SQLModel, table=True):
    __tablename__ = "profile_facts"
    __table_args__ = (
        Index("ix_profile_facts_kind", "kind"),
        Index("ix_profile_facts_active", "active"),
    )

    # Deterministic from the fact's natural key, so the same fact keeps its id
    # across saves and reorderings (see shared/profile/facts.py).
    id: str = Field(primary_key=True)
    kind: FactKind
    text: str = Field(sa_column=Column(Text))
    path: str
    parent_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Contact details: needed to fill forms, never sent to an LLM.
    pii: bool = False
    # The CV line the fact was found on, when it came from an import.
    evidence: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # True once the human finalized a profile containing this fact.
    verified: bool = False
    active: bool = True
    first_profile_version: int
    last_profile_version: int
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

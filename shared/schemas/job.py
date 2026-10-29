"""Job lead — the scraper writes it; matcher/applier read it."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field, SQLModel


class JobStatus(StrEnum):
    NEW = "new"
    SCORED = "scored"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"

def make_job_id(source: str, external_id: str) -> str:
    """Deterministic id: same job from same source always hashes to the same id."""
    return hashlib.sha256(f"{source}|{external_id}".encode()).hexdigest()

def _now() -> datetime:
    return datetime.now(UTC)


class Job(SQLModel, table=True):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_source_external", "source", "external_id", unique=True),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_company_norm", "company_norm"),
        Index("ix_jobs_duplicate_of", "duplicate_of"),
    )

    id: str = Field(primary_key=True)
    source: str
    external_id: str
    title: str
    company: str
    location_raw: str = ""
    location_norm: str | None = None
    remote: bool | None = None

    description_raw: str = Field(default="", sa_column=Column(Text))
    url: str = ""

    posted_at: datetime | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None

    skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    status: JobStatus = Field(default=JobStatus.NEW)
    score: float | None = None
    score_breakdown: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    first_seen_at: datetime = Field(default_factory=_now)
    last_seen_at: datetime = Field(default_factory=_now)

    # Phase 4 (additive): canonical keys for dedup, and the canonical job this
    # one duplicates. Downstream modules skip rows where duplicate_of is set.
    title_norm: str | None = None
    company_norm: str | None = None
    duplicate_of: str | None = None

    # Phase 6 (additive): matcher's structured reading of the JD (must-haves,
    # keywords). Cached so tailoring a resume twice costs one extraction.
    requirements: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
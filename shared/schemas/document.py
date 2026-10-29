"""Tailored application documents. The matcher writes them; the applier reads
the PDF path to upload. Frozen: additive changes only."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Index
from sqlmodel import Field, SQLModel


class DocumentKind(StrEnum):
    RESUME = "resume"
    COVER_LETTER = "cover_letter"


def make_document_id(job_id: str, kind: DocumentKind) -> str:
    """One current document per (job, kind): re-tailoring replaces it in place."""
    return hashlib.sha256(f"{job_id}|{kind.value}".encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class TailoredDocument(SQLModel, table=True):
    __tablename__ = "tailored_documents"
    __table_args__ = (Index("ix_tailored_documents_job", "job_id"),)

    id: str = Field(primary_key=True)
    job_id: str = Field(foreign_key="jobs.id")
    kind: DocumentKind
    pdf_path: str
    html_path: str
    # Every profile fact the document states, so each line traces to a fact.
    fact_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # True only when the final text passed the verifier with no violations.
    verified: bool = False
    # Violations of the rejected LLM drafts, kept for Phase 11 diagnosis.
    violations: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    # True when every LLM draft failed and the profile-only version was used.
    fallback: bool = False
    attempts: int = 0
    prompt_version: str | None = None
    model: str | None = None
    profile_version: int | None = None
    created_at: datetime = Field(default_factory=_now)

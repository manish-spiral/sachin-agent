"""One application attempt. The applier writes it."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class ApplicationStatus(StrEnum):
    PENDING = "pending"
    PAUSED = "paused"
    SUBMITTED = "submitted"
    FAILED = "failed"
    ABANDONED = "abandoned"


class PauseReason(StrEnum):
    CAPTCHA = "captcha"
    LOGIN = "login"
    CONSENT = "consent"
    UNKNOWN_FIELD = "unknown_field"
    ADAPTER_APPROVAL = "adapter_approval"


def _now() -> datetime:
    return datetime.now(UTC)


class Application(SQLModel, table=True):
    __tablename__ = "applications"
    __table_args__ = (
        Index("ix_applications_job_id", "job_id"),
        Index("ix_applications_status", "status"),
    )

    id: str = Field(primary_key=True)
    job_id: str = Field(foreign_key="jobs.id")
    status: ApplicationStatus = Field(default=ApplicationStatus.PENDING)
    pause_reason: PauseReason | None = None
    adapter_version_id: str | None = None
    resume_path: str | None = None
    cover_letter_path: str | None = None
    browser_state_path: str | None = None
    last_step: str | None = None
    submitted_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
"""Event-sourced failure log. Emitted from Phase 3 onwards; classified in Phase 11."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field, SQLModel


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    SELECTOR_DRIFT = "selector_drift"
    SCHEMA_DRIFT = "schema_drift"
    RATE_LIMIT = "rate_limit"
    AUTH_WALL = "auth_wall"
    CAPTCHA = "captcha"
    CONSENT = "consent"
    UNSEEN_SITE = "unseen_site"
    UNKNOWN_FIELD = "unknown_field"
    NOVEL_INTERACTION = "novel_interaction"
    SEMANTIC_MISMATCH = "semantic_mismatch"
    UNKNOWN = "unknown"


def _now() -> datetime:
    return datetime.now(UTC)


class FailureEvent(SQLModel, table=True):
    __tablename__ = "failures"
    __table_args__ = (
        Index("ix_failures_class", "failure_class"),
        Index("ix_failures_domain", "domain"),
    )

    id: str = Field(primary_key=True)
    source_module: str
    domain: str | None = None
    phase: str
    job_id: str | None = None
    application_id: str | None = None
    snapshot_id: str | None = None
    error_type: str
    error_message: str = Field(default="", sa_column=Column(Text))
    context: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    failure_class: FailureClass | None = None
    diagnosed_at: datetime | None = None
    # Phase 11 (additive): {"method": rule|llm, "confidence", "evidence",
    # "emitted_class", "prompt_version", "model"}. failure_class is then the
    # diagnosed class; the emitter's provisional guess stays here.
    diagnosis: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
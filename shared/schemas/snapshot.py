"""Index of captured browser/API snapshots (Phase 10+)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class SnapshotIndex(SQLModel, table=True):
    __tablename__ = "snapshots"
    __table_args__ = (
        Index("ix_snapshots_domain", "domain"),
        Index("ix_snapshots_run", "run_id"),
    )

    id: str = Field(primary_key=True)
    run_id: str
    domain: str
    url: str
    intent: str
    step_index: int = 0
    html_path: str
    a11y_path: str | None = None
    har_path: str | None = None
    screenshot_path: str | None = None
    trace_path: str | None = None
    page_signature: str | None = None
    created_at: datetime = Field(default_factory=_now)
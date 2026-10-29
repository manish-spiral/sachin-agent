"""One invocation of the full pipeline (Phase 9): what ran, how it ended, and
the before/after counts that make up the nightly summary."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Index
from sqlmodel import Field, SQLModel


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"  # some stages failed, the rest ran
    KILLED = "killed"
    FAILED = "failed"


def _now() -> datetime:
    return datetime.now(UTC)


class PipelineRun(SQLModel, table=True):
    __tablename__ = "pipeline_runs"
    __table_args__ = (Index("ix_pipeline_runs_started", "started_at"),)

    id: str = Field(primary_key=True)
    trigger: str = "manual"  # manual | timer
    status: RunStatus = RunStatus.RUNNING
    # [{"name", "argv", "exit_code", "seconds", "output_tail"}]
    stages: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    summary: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None

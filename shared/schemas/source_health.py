"""Per-source success/failure counters. Feeds self-improvement prioritization."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text
from sqlmodel import Column, Field, SQLModel


class SourceHealth(SQLModel, table=True):
    __tablename__ = "source_health"

    source: str = Field(primary_key=True)
    last_success: datetime | None = None
    last_error: datetime | None = None
    last_error_message: str | None = Field(default=None, sa_column=Column(Text))
    success_count: int = 0
    failure_count: int = 0
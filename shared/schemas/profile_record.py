"""Version history for the profile — one row per save."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class ProfileRecord(SQLModel, table=True):
    __tablename__ = "profile_records"
    __table_args__ = (Index("ix_profile_records_created", "created_at"),)

    id: str = Field(primary_key=True)
    version: int
    profile_json: dict[str, Any] = Field(sa_column=Column(JSON))
    source: str  # "init" | "import" | "manual" | "restore"
    notes: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=_now)
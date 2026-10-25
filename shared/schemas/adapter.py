"""Versioned adapters (Phase 13+). Enables rollback and provenance."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class AdapterVersion(SQLModel, table=True):
    __tablename__ = "adapter_versions"
    __table_args__ = (
        Index("ix_adapters_domain", "domain"),
        Index("ix_adapters_active", "domain", "is_active"),
    )

    id: str = Field(primary_key=True)
    domain: str
    version: int
    parent_version_id: str | None = None
    adapter_code_path: str
    fix_summary: str = Field(default="", sa_column=Column(Text))
    evidence: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    snapshot_pass_rate: float = 0.0
    source: str = "synthesized"  # synthesized | human_approved
    promoted_at: datetime = Field(default_factory=_now)
    is_active: bool = False
"""Reusable procedure learned from past repairs (Phase 14+)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, Index, Text
from sqlmodel import Field, SQLModel


class Skill(SQLModel, table=True):
    __tablename__ = "skills"
    __table_args__ = (Index("ix_skills_signature", "domain_signature"),)

    id: str = Field(primary_key=True)
    domain_signature: str
    goal: str
    trigger_conditions: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    procedure: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    source: str = "synthesized"  # synthesized | human_approved | learned_from_human
    success_count: int = 0
    failure_count: int = 0
    last_validated: datetime | None = None
    version: int = 1
    test_snapshot_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    notes: str = Field(default="", sa_column=Column(Text))
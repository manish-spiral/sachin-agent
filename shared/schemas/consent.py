"""Per-domain consent decisions made by the human (constraint 8).

The agent never accepts a consent wall or a legal acknowledgement on its own.
The human decides once per domain and prompt; the agent then replays exactly
that decision on that domain, and nothing else.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Column, Index, Text
from sqlmodel import Field, SQLModel


class ConsentKind(StrEnum):
    BANNER = "banner"  # cookie / tracking consent walls
    ACKNOWLEDGEMENT = "acknowledgement"  # form questions: "I acknowledge the privacy notice"


def make_consent_id(domain: str, kind: ConsentKind, prompt: str) -> str:
    return hashlib.sha256(f"{domain}|{kind.value}|{prompt.strip().lower()}".encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class ConsentChoice(SQLModel, table=True):
    __tablename__ = "consent_choices"
    __table_args__ = (Index("ix_consent_choices_domain", "domain"),)

    id: str = Field(primary_key=True)
    domain: str
    kind: ConsentKind
    prompt: str = Field(sa_column=Column(Text))
    # The exact button text / option the human chose ("Reject all", "I acknowledge").
    choice: str
    decided_by: str = "human"
    decided_at: datetime = Field(default_factory=_now)

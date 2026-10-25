"""Interface every source implements. Empty in Phase 0."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class RawJob:
    """Unnormalized job as returned by a source."""

    source: str
    external_id: str
    payload: dict[str, Any]


class BaseSource(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch(self) -> list[RawJob]: ...

    @abstractmethod
    async def normalize(self, raw: RawJob):  # -> Job (import at use site)
        ...
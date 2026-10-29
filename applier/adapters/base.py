"""The frozen adapter interface. Every site adapter — hand-written, healed or
synthesized — is driven through exactly these five calls."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from applier.context import ApplicantContext
from applier.session import BrowserSession


@dataclass
class FieldResult:
    intent: str
    label: str
    status: str  # filled | skipped | unknown | missing | consent
    value: str = ""
    detail: str = ""
    options: list[str] = field(default_factory=list)


@dataclass
class FillReport:
    fields: list[FieldResult] = field(default_factory=list)

    def by_status(self, status: str) -> list[FieldResult]:
        return [f for f in self.fields if f.status == status]

    @property
    def needs_human(self) -> list[FieldResult]:
        """Required fields the agent could not (or may not) answer itself."""
        return [f for f in self.fields if f.status in {"unknown", "missing", "consent"}]


@dataclass
class PageInfo:
    url: str
    title: str
    questions: list[str] = field(default_factory=list)


class Adapter(ABC):
    domain: str

    @abstractmethod
    def init(self, session: BrowserSession, url: str) -> None:
        """Navigate to the application form (opening it if the page hides it)."""

    @abstractmethod
    def detect(self, session: BrowserSession) -> bool:
        """True if the current page is a form this adapter can fill."""

    @abstractmethod
    def fill(self, session: BrowserSession, applicant: ApplicantContext) -> FillReport:
        """Fill everything answerable from facts. Never submits."""

    @abstractmethod
    def extract(self, session: BrowserSession) -> PageInfo:
        """Describe the page (title, questions) for logs and diagnosis."""

    @abstractmethod
    def submit(self, session: BrowserSession) -> bool:
        """Submit and return True only if the site confirmed the submission."""

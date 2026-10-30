"""Logged-in, read-only browsing of job sites — opt-in, human-paced.

A deliberate, user-approved exception to "use public APIs only": these sites
have no public jobs API. Guardrails, all enforced here:
  - runs only with an explicit opt-in flag (`browse --ok`);
  - read-only: it opens search pages and reads job cards, nothing else;
  - human pace: random pauses between actions, page and detail caps, and at
    most one run per site per MIN_INTERVAL_HOURS;
  - any checkpoint, CAPTCHA or login wall stops the run (snapshot + notify);
    nothing is solved, retried in a loop, or disguised.
"""

from __future__ import annotations

import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

SPECS = Path(__file__).parent / "specs"


class ListingSpec(BaseModel):
    """Where job cards live on a site's search page. Data, not code, so the
    Phase 13 healer can repair it like an adapter spec."""

    model_config = ConfigDict(extra="forbid")
    site: str
    domain: str
    version: int = 1
    card: str
    id_attributes: list[str]
    link: str
    title: str
    company: str
    location: str
    list_container: str
    description: str

    @classmethod
    def builtin(cls, site: str) -> ListingSpec:
        return cls.model_validate_json((SPECS / f"{site}.json").read_text(encoding="utf-8"))


@dataclass
class Card:
    external_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""


class Pace:
    """Human-paced waits. Not an evasion technique: it keeps load at the level
    of one person reading, which is the point of these guardrails."""

    def __init__(self, low: float = 4.0, high: float = 9.0, *, sleep=time.sleep) -> None:
        self.low, self.high, self._sleep = low, high, sleep

    def wait(self, factor: float = 1.0) -> None:
        self._sleep(random.uniform(self.low, self.high) * factor)


class Checkpoint(RuntimeError):
    """The site asked for verification or a login: stop, never work around it."""


class BrowseSite(ABC):
    name: str
    domain: str
    login_url: str
    # Minimum hours between runs: about once a day.
    min_interval_hours: float = 20.0

    def __init__(self, spec: ListingSpec | None = None, *, remote_country: str = "India") -> None:
        self.spec = spec or ListingSpec.builtin(self.name)
        # "Remote" searches mean remote roles open to candidates in this country.
        self.remote_country = remote_country

    @abstractmethod
    def search_url(self, query: str, location: str, page: int) -> str:
        """The site's own search URL for one results page (0-based)."""

    @abstractmethod
    def checkpoint(self, page: Page) -> str | None:
        """Why the site is not showing results (login/verification), or None."""

    @abstractmethod
    def is_logged_in(self, page: Page) -> bool:
        """True when the saved session is signed in."""

    def job_url(self, external_id: str) -> str:
        return f"https://www.{self.domain}/jobs/view/{external_id}/"


def _text(locator: Locator) -> str:
    try:
        return re.sub(r"\s+", " ", locator.first.inner_text(timeout=1_000)).strip() if locator.count() else ""
    except Exception:
        return ""

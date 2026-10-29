"""Interface every source implements, plus a retrying JSON fetcher."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from shared.schemas.failure import FailureClass
from shared.schemas.job import Job

# A hostile or buggy Retry-After must not stall a nightly run for hours.
MAX_RETRY_AFTER_SECONDS = 120


@dataclass
class RawJob:
    """Unnormalized job as returned by a source."""

    source: str
    company: str
    external_id: str
    payload: dict[str, Any] = field(default_factory=dict)


class HttpFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        failure_class: FailureClass = FailureClass.UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.failure_class = failure_class


class BaseSource(ABC):
    """One ATS or job board. Stateless — the runner passes an httpx client in.

    A "board" is whatever the source is queried by: a company slug for an ATS,
    a tag/category (or "all") for an aggregator.
    """

    name: str = "base"
    domain: str = "unknown"
    # Boards used when scraper/companies.json has no entry for this source.
    default_boards: tuple[str, ...] = ()
    # Lower ranks win when dedup picks the canonical copy. Direct ATS postings
    # beat aggregator reposts because they carry the real apply URL.
    dedup_rank: int = 0
    # Minimum hours between successful runs of one board, from the board's ToS.
    min_interval_hours: float | None = None

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient, company: str) -> list[RawJob]:
        """Return raw jobs for one board."""

    async def enrich(
        self, client: httpx.AsyncClient, raws: list[RawJob]
    ) -> tuple[list[RawJob], list[tuple[RawJob, Exception]]]:
        """Fetch per-job details the list endpoint omits. Runs after --limit.

        Returns every raw (enriched where possible) plus the per-job errors, so
        one bad detail page costs a description, not the whole board.
        """
        return raws, []

    @abstractmethod
    def normalize(self, raw: RawJob) -> Job:
        """Map a RawJob into the frozen Job schema."""

    async def get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> Any:
        """GET JSON with retries for 429 / 5xx. Raises HttpFetchError otherwise."""
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                raise HttpFetchError(
                    f"HTTP transport error: {exc}",
                    failure_class=FailureClass.TRANSIENT,
                ) from exc

            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt < max_retries:
                    retry_after = response.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                    await asyncio.sleep(min(delay, MAX_RETRY_AFTER_SECONDS))
                    continue
                cls = FailureClass.RATE_LIMIT if response.status_code == 429 else FailureClass.TRANSIENT
                raise HttpFetchError(
                    f"HTTP {response.status_code} from {url}",
                    status_code=response.status_code,
                    failure_class=cls,
                )

            if response.status_code >= 400:
                raise HttpFetchError(
                    f"HTTP {response.status_code} from {url}",
                    status_code=response.status_code,
                    failure_class=FailureClass.UNKNOWN,
                )

            try:
                return response.json()
            except ValueError as exc:
                raise HttpFetchError(
                    f"Invalid JSON from {url}",
                    status_code=response.status_code,
                    failure_class=FailureClass.SCHEMA_DRIFT,
                ) from exc

        raise HttpFetchError(
            f"Exhausted retries for {url}: {last_error}",
            failure_class=FailureClass.TRANSIENT,
        )
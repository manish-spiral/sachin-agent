"""Run an adapter against every stored form snapshot of a domain: a pass rate.

This is the gate every adapter change must clear before promotion (Phase 13+).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlmodel import col, or_, select

from applier.adapters.form import FormAdapter
from sandbox.offline import OfflineBrowser
from sandbox.replay import Assertion, ReplayResult, replay
from sandbox.suites import FORM_INTENTS, suite_for
from shared.db.engine import get_session
from shared.schemas.snapshot import SnapshotIndex


@dataclass
class SuiteResult:
    domain: str
    results: list[ReplayResult] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def form_snapshots(domain: str, *, limit: int | None = None) -> list[SnapshotIndex]:
    """Form-page snapshots for a domain (suffix match), newest first, one per
    distinct page so repeated captures of the same form don't skew the rate."""
    query = (
        select(SnapshotIndex)
        .where(col(SnapshotIndex.intent).in_(FORM_INTENTS))
        .where(or_(col(SnapshotIndex.domain) == domain, col(SnapshotIndex.domain).endswith("." + domain)))
        .order_by(col(SnapshotIndex.created_at).desc())
    )
    with get_session() as s:
        rows = list(s.exec(query).all())
    seen: set[str] = set()
    unique = []
    for row in rows:
        if row.url not in seen:
            seen.add(row.url)
            unique.append(row)
    return unique[:limit] if limit else unique


def run_suite(domain: str, adapter: FormAdapter, *, assertions: list[Assertion] | None = None,
              snapshots: list[SnapshotIndex] | None = None) -> SuiteResult:
    started = time.monotonic()
    chosen = snapshots if snapshots is not None else form_snapshots(domain)
    checks = assertions or suite_for(domain)
    result = SuiteResult(domain)
    with OfflineBrowser() as browser:
        for snapshot in chosen:
            result.results.append(replay(snapshot, adapter, checks, browser=browser))
    result.seconds = round(time.monotonic() - started, 2)
    return result

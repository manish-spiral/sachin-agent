"""Replay one adapter against one stored snapshot, offline, and judge it.

An assertion gets the adapter, a session over the offline page and the
snapshot, and returns None (pass) or a failure message. Nothing here can
reach the live site: pages come from sandbox/offline.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from applier.adapters.form import FormAdapter
from sandbox.offline import OfflineBrowser
from shared.browser.session import BrowserSession
from shared.schemas.snapshot import SnapshotIndex

if TYPE_CHECKING:
    from playwright.sync_api import Page


class ReplaySession(BrowserSession):
    """A BrowserSession over an already-open offline page. It cannot navigate:
    replay judges the captured page, never a live one."""

    def __init__(self, page: Page) -> None:
        super().__init__(Path("."), headless=True)
        self._page = page

    def goto(self, url: str) -> None:
        raise RuntimeError(f"replay is offline; refusing to navigate to {url}")


Assertion = Callable[[FormAdapter, ReplaySession, SnapshotIndex], str | None]


@dataclass
class ReplayResult:
    snapshot_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0


def run_assertions(adapter: FormAdapter, session: ReplaySession, snapshot: SnapshotIndex,
                   assertions: list[Assertion]) -> list[str]:
    failures = []
    for check in assertions:
        try:
            message = check(adapter, session, snapshot)
        except Exception as exc:  # a crashing check is a failing check
            message = f"{getattr(check, '__name__', 'assertion')} raised {type(exc).__name__}: {exc}"
        if message:
            failures.append(message)
    return failures


def replay(snapshot: SnapshotIndex, adapter: FormAdapter, assertions: list[Assertion], *,
           browser: OfflineBrowser | None = None, javascript: bool = False) -> ReplayResult:
    started = time.monotonic()
    owned = browser is None
    browser = browser or OfflineBrowser()
    try:
        with browser.page(snapshot, javascript=javascript) as page:
            failures = run_assertions(adapter, ReplaySession(page), snapshot, assertions)
    finally:
        if owned:
            browser.close()
    return ReplayResult(snapshot.id, not failures, failures, round(time.monotonic() - started, 2))

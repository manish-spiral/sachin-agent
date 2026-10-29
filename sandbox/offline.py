"""Open a snapshot in a browser with no live network.

The snapshot's URL is served from its saved DOM; every other request is
answered from the run's HAR when it has one, and aborted otherwise. Nothing
reaches the live site — that is the whole point of the sandbox.

Two modes: static (JavaScript off; the saved post-settle DOM exactly as
captured) and scripted (JavaScript on, scripts served from the HAR).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from shared.db.engine import get_session
from shared.schemas.snapshot import SnapshotIndex

if TYPE_CHECKING:
    from playwright.sync_api import Page


class SnapshotNotFound(LookupError):
    pass


def load_snapshot(snapshot_id: str) -> SnapshotIndex:
    with get_session() as s:
        snap = s.get(SnapshotIndex, snapshot_id)
    if snap is None:
        raise SnapshotNotFound(snapshot_id)
    return snap


@contextmanager
def offline_page(snapshot: SnapshotIndex, *, javascript: bool = False) -> Iterator[Page]:
    from playwright.sync_api import sync_playwright

    dom = Path(snapshot.html_path).read_text(encoding="utf-8")
    har = Path(snapshot.har_path) if snapshot.har_path else None
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(java_script_enabled=javascript, service_workers="block")
            context.route("**/*", lambda route: route.abort())
            if har is not None and har.exists():
                context.route_from_har(str(har), not_found="abort")
            # Registered last, so it wins for the page itself.
            context.route(snapshot.url, lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=dom))
            page = context.new_page()
            page.goto(snapshot.url, wait_until="domcontentloaded")
            yield page
        finally:
            browser.close()

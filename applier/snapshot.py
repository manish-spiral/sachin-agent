"""SnapshotRecorder: an offline-replayable record of every browser step.

Per run:  snapshots/<run_id>/network.har, trace.zip
Per step: snapshots/<run_id>/<NN>/dom.html, aria.yaml, screenshot.png, meta.json
plus one SnapshotIndex row with the page signature (shared/signature.py).
Fixes are later tested against these files, never against the live site.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from applier.session import BrowserSession
from shared.db.engine import get_session
from shared.paths import snapshot_run_dir
from shared.schemas.snapshot import SnapshotIndex
from shared.signature import html_features, page_signature

SETTLE_MS = 300


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}"


class SnapshotRecorder:
    def __init__(self, run_id: str, domain: str) -> None:
        self.run_id = run_id
        self.domain = domain
        self.run_dir = snapshot_run_dir(run_id)
        self.har_path = self.run_dir / "network.har"
        self.trace_path = self.run_dir / "trace.zip"
        self.step = 0
        self.last: SnapshotIndex | None = None

    def attach(self, session: BrowserSession) -> BrowserSession:
        """Configure an unstarted session to record the run's HAR and trace."""
        session.record_har = self.har_path
        session.record_trace = self.trace_path
        return session

    def capture(self, session: BrowserSession, intent: str) -> SnapshotIndex:
        self.step += 1
        step_dir = self.run_dir / f"{self.step:02d}"
        step_dir.mkdir(parents=True, exist_ok=True)
        page = session.page
        page.wait_for_timeout(SETTLE_MS)  # let late DOM updates land

        dom = str(page.evaluate("document.documentElement.outerHTML"))
        (step_dir / "dom.html").write_text(dom, encoding="utf-8")
        a11y_path: Path | None = step_dir / "aria.yaml"
        try:
            a11y_path.write_text(page.locator("body").aria_snapshot(), encoding="utf-8")  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning(f"aria snapshot failed: {exc}")
            a11y_path = None
        shot = session.screenshot(step_dir / "screenshot.png")
        (step_dir / "meta.json").write_text(
            json.dumps({"url": page.url, "title": page.title(), "intent": intent}, indent=2), encoding="utf-8"
        )

        features = html_features(dom)
        row = SnapshotIndex(
            id=f"{self.run_id}-{self.step:02d}",
            run_id=self.run_id,
            domain=self.domain,
            url=page.url,
            intent=intent,
            step_index=self.step,
            html_path=str(step_dir / "dom.html"),
            a11y_path=str(a11y_path) if a11y_path else None,
            har_path=str(self.har_path),
            screenshot_path=str(shot),
            trace_path=str(self.trace_path),
            page_signature=page_signature(features),
            features=features,
        )
        with get_session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
        self.last = row
        return row

"""Run the browse cluster: search pages → job cards → jobs, at human pace.

Read-only by construction: the only navigation is to the site's own search
URLs; the only interaction is scrolling cards into view and clicking a card to
show its description. Any checkpoint ends the run for that site.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from scraper.browse.base import BrowseSite, Card, Checkpoint, Pace, _text
from scraper.normalize import apply_normalization
from scraper.runner import run_dedup
from shared.browser.session import BrowserSession
from shared.browser.snapshot import SnapshotRecorder, new_run_id
from shared.config import get_settings
from shared.db.engine import get_session
from shared.db.failures import emit_failure
from shared.db.source_health import record_source_failure, record_source_success
from shared.db.upsert import upsert_job
from shared.killswitch import is_killed
from shared.notify import notify
from shared.schemas.failure import FailureClass
from shared.schemas.job import Job, make_job_id
from shared.schemas.source_health import SourceHealth

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

CONFIG_PATH = Path("scraper/browse.json")
DEFAULT_QUERIES = ["AI engineer"]
DEFAULT_LOCATIONS = ["Delhi, India", "Bengaluru, India", "Remote"]
# Ids appear as urn:li:jobPosting:123, /jobs/view/123, /jobs/view/some-title-at-co-123, ?currentJobId=123.
_JOB_ID_RE = re.compile(r"(?:jobPosting:|/jobs/view/(?:[^/?#]*-)?|currentJobId=)(\d+)")
LOGIN_TIMEOUT_SECONDS = 300

SessionFactory = Callable[[bool], BrowserSession]  # headed? → unstarted session


@dataclass
class BrowseConfig:
    queries: list[str] = field(default_factory=lambda: list(DEFAULT_QUERIES))
    locations: list[str] = field(default_factory=lambda: list(DEFAULT_LOCATIONS))
    pages: int = 2  # results pages per query × location
    details: int = 20  # descriptions opened per site per run
    remote_country: str = "India"

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> BrowseConfig:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class BrowseReport:
    site: str
    pages: int = 0
    cards: int = 0
    stored: int = 0
    descriptions: int = 0
    stopped: str | None = None
    skipped: str | None = None


def default_session(headed: bool) -> BrowserSession:
    return BrowserSession(Path(get_settings().browser_profile_dir), headless=not headed)


def _health_key(site: BrowseSite) -> str:
    return f"{site.name}:browse"


def too_soon(site: BrowseSite) -> bool:
    with get_session() as s:
        health = s.get(SourceHealth, _health_key(site))
    if health is None or health.last_success is None:
        return False
    last = health.last_success.replace(tzinfo=health.last_success.tzinfo or UTC)
    return datetime.now(UTC) - last < timedelta(hours=site.min_interval_hours)


def _card_id(card: Locator, site: BrowseSite) -> str | None:
    for attr in site.spec.id_attributes:
        value = card.get_attribute(attr)
        if value:
            match = re.search(r"\d+", value)
            if match:
                return match.group(0)
    link = card.locator(site.spec.link)
    href = link.first.get_attribute("href") if link.count() else None
    match = _JOB_ID_RE.search(href or "")
    return match.group(1) if match else None


def _first_line(text: str) -> str:
    return text.split("\n")[0].strip()


def read_card(card: Locator, site: BrowseSite) -> Card | None:
    job_id = _card_id(card, site)
    if job_id is None:
        return None
    title_el = card.locator(site.spec.title)
    title = (title_el.first.get_attribute("aria-label") if title_el.count() else None) or _text(title_el)
    if not title:
        title = _first_line(card.locator(site.spec.link).first.inner_text()) if card.locator(site.spec.link).count() else ""
    title = re.sub(r"\s+with verification$", "", title.strip(), flags=re.I)
    if not title:
        return None
    return Card(
        external_id=job_id,
        title=title,
        company=_text(card.locator(site.spec.company)),
        location=_text(card.locator(site.spec.location)),
        url=site.job_url(job_id),
    )


def _fallback_cards(page: Page, site: BrowseSite) -> list[Card]:
    """When the spec's card selector matches nothing, read any job links."""
    cards: dict[str, Card] = {}
    for link in page.locator(site.spec.link).all():
        match = _JOB_ID_RE.search(link.get_attribute("href") or "")
        if not match or match.group(1) in cards:
            continue
        container = link.locator("xpath=ancestor::li[1]")
        lines = [x.strip() for x in (container.first.inner_text() if container.count() else link.inner_text()).split("\n")
                 if x.strip()]
        title = (link.get_attribute("aria-label") or (lines[0] if lines else "")).strip()
        if title:
            cards[match.group(1)] = Card(match.group(1), title, lines[1] if len(lines) > 1 else "",
                                         lines[2] if len(lines) > 2 else "", site.job_url(match.group(1)))
    return list(cards.values())


def _has_description(job_id: str) -> bool:
    with get_session() as s:
        job = s.get(Job, job_id)
    return bool(job and job.description_raw)


def _read_description(page: Page, card: Locator, site: BrowseSite, pace: Pace) -> str:
    # Click a non-link part of the card (company line), so the details pane
    # opens without following the job link: browsing stays on the search page.
    target = card.locator(site.spec.company)
    (target.first if target.count() else card).click()
    pace.wait(0.6)
    description = page.locator(site.spec.description)
    try:
        description.first.wait_for(state="visible", timeout=10_000)
    except Exception:
        return ""
    return re.sub(r"\s+", " ", description.first.inner_text()).strip()


def _store(card: Card, site: BrowseSite, searched_remote: bool) -> Job:
    job = Job(
        id=make_job_id(site.name, card.external_id),
        source=site.name,
        external_id=card.external_id,
        title=card.title,
        company=card.company,
        location_raw=card.location,
        remote=True if searched_remote or "remote" in card.location.lower() else None,
        description_raw=card.description,
        url=card.url,
    )
    with get_session() as s:
        return upsert_job(s, apply_normalization(job))


def _stop(site: BrowseSite, session: BrowserSession, recorder: SnapshotRecorder, reason: str,
          report: BrowseReport) -> None:
    snap = recorder.capture(session, "browse_checkpoint")
    challenge = re.search(r"challenge|captcha|human", reason, re.I)
    emit_failure(source_module="scraper", phase="browse_checkpoint", error=Checkpoint(reason),
                 failure_class=FailureClass.CAPTCHA if challenge else FailureClass.AUTH_WALL,
                 domain=site.domain, snapshot_id=snap.id, context={"site": site.name, "url": session.page.url})
    record_source_failure(_health_key(site), reason)
    notify(f"⏸ {site.name} browsing stopped: {reason}. Nothing was retried. "
           f"Log in again with `sachin-agent-scraper browse-login {site.name}` if needed.",
           photo=Path(snap.screenshot_path) if snap.screenshot_path else None)
    report.stopped = reason
    logger.warning(f"{site.name}: stopped at checkpoint: {reason}")


def browse_site(site: BrowseSite, config: BrowseConfig, *, session_factory: SessionFactory = default_session,
                pace: Pace | None = None, force: bool = False) -> BrowseReport:
    pace = pace or Pace()
    report = BrowseReport(site.name)
    if not force and too_soon(site):
        report.skipped = f"browsed less than {site.min_interval_hours:g}h ago (use --force)"
        return report

    recorder = SnapshotRecorder(new_run_id(f"browse-{site.name}"), site.domain)
    details_left = config.details
    with recorder.attach(session_factory(False)) as session:
        page = session.page
        for query in config.queries:
            for location in config.locations:
                for page_no in range(config.pages):
                    if is_killed():
                        report.stopped = "kill switch"
                        return report
                    session.goto(site.search_url(query, location, page_no))
                    pace.wait()
                    reason = site.checkpoint(page)
                    if reason:
                        _stop(site, session, recorder, reason, report)
                        return report
                    report.pages += 1

                    found: list[Card] = []
                    cards = page.locator(site.spec.card)
                    for i in range(cards.count()):
                        card = cards.nth(i)
                        card.scroll_into_view_if_needed()  # cards render only near the viewport
                        parsed = read_card(card, site)
                        if parsed is None:
                            continue
                        if details_left > 0 and not _has_description(make_job_id(site.name, parsed.external_id)):
                            parsed.description = _read_description(page, card, site, pace)
                            details_left -= 1
                            report.descriptions += bool(parsed.description)
                        found.append(parsed)
                    if not found:
                        found = _fallback_cards(page, site)
                    recorder.capture(session, "browse_list")
                    for parsed in found:
                        _store(parsed, site, location.strip().lower() == "remote")
                    report.cards += len(found)
                    report.stored += len(found)
                    logger.info(f"{site.name} {query!r} @ {location} p{page_no + 1}: {len(found)} jobs")
                    if not found:
                        break  # no more results for this search
    record_source_success(_health_key(site))
    run_dedup()
    return report


def browse_login(site: BrowseSite, *, session_factory: SessionFactory = default_session,
                 timeout: float = LOGIN_TIMEOUT_SECONDS, poll: Callable[[float], None] | None = None) -> bool:
    """Open the site's login page in a visible browser and wait for the human
    to sign in. Credentials are typed by the human, never seen or stored here;
    the browser profile keeps the session cookies."""
    import time

    poll = poll or time.sleep
    with session_factory(True) as session:
        session.goto(site.login_url)
        waited = 0.0
        while waited < timeout:
            if site.is_logged_in(session.page):
                return True
            poll(2.0)
            waited += 2.0
    return False

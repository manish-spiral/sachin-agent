"""Browse cluster: logged-in, read-only, human-paced job reading — offline."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlmodel import select
from typer.testing import CliRunner

from scraper.browse.base import Pace
from scraper.browse.linkedin import LinkedInSite
from scraper.browse.runner import BrowseConfig, browse_login, browse_site
from scraper.cli import app as scraper_cli
from shared.browser.session import BrowserSession
from shared.db.engine import get_session
from shared.killswitch import kill_file
from shared.schemas.failure import FailureClass, FailureEvent
from shared.schemas.job import Job
from shared.schemas.source_health import SourceHealth

# Logged-in search layout (written for tests): occludable cards, a details
# pane filled in when a card is clicked, and LinkedIn's hidden sensor iframe.
SEARCH = """<!doctype html><html><head><title>AI engineer Jobs</title></head><body>
<iframe id="humanThirdPartyIframe" src="https://li.protechts.net/index.html?uc=scraping"
        aria-hidden="true" style="width:0;height:0;border:0"></iframe>
<div class="scaffold-layout__list"><ul>{cards}</ul></div>
<div class="jobs-search__job-details--container"><div id="job-details"></div></div>
<script>
document.querySelectorAll('[data-occludable-job-id]').forEach(li => li.addEventListener('click', () => {{
  document.getElementById('job-details').textContent = 'Build LLM systems with Python. Job ' + li.dataset.occludableJobId;
}}));
</script></body></html>"""
CARD = """<li data-occludable-job-id="{id}"><div class="job-card-container">
<a class="job-card-container__link" aria-label="{title}" href="/jobs/view/{id}/?trk=x">{title}</a>
<div class="artdeco-entity-lockup__subtitle"><span>{company}</span></div>
<div class="artdeco-entity-lockup__caption">{location}</div></div></li>"""
CHECKPOINT = "<html><body><main><h1>Let's do a quick security check</h1></main></body></html>"


def _page(jobs: list[tuple[str, str, str, str]]) -> str:
    return SEARCH.format(cards="".join(CARD.format(id=i, title=t, company=c, location=loc) for i, t, c, loc in jobs))


def _results(url: str) -> str:
    if "/jobs/search/" not in url:
        return "<p>not a search page</p>"  # the read-only assertion catches any visit here
    q = parse_qs(urlparse(url).query)
    if q.get("start"):
        return _page([])  # page 2: no more results
    if q.get("f_WT") == ["2"]:
        return _page([("300", "Remote AI Engineer", "Acme", "India (Remote)")])
    city = q["location"][0].split(",")[0]
    base = {"Delhi": 1000, "Bengaluru": 2000}[city]  # LinkedIn job ids are numeric
    return _page([(str(base + 1), f"AI Engineer, {city}", "Globex", f"{city}, India"),
                  (str(base + 2), "Sr. ML Engineer", "Initech", f"{city}, India")])


class Sessions:
    def __init__(self, tmp: Path, route=_results, logged_in_after: int | None = None) -> None:
        self.tmp, self.route, self.logged_in_after, self.visited = tmp, route, logged_in_after, []

    def __call__(self, headed: bool) -> BrowserSession:
        outer = self

        class Offline(BrowserSession):
            def start(self) -> BrowserSession:
                super().start()

                def serve(route) -> None:
                    url = route.request.url
                    if url.startswith("https://www.linkedin.com/"):
                        outer.visited.append(url)
                        route.fulfill(status=200, content_type="text/html", body=outer.route(url))
                    else:
                        route.abort()  # the sensor and anything else stays offline

                self.context.route("**/*", serve)  # type: ignore[union-attr]
                return self

        return Offline(self.tmp / "profile", headless=not headed)


@pytest.fixture
def no_wait() -> Pace:
    waits: list[float] = []
    return Pace(sleep=waits.append)


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr("scraper.browse.runner.notify", lambda text, **kw: messages.append(text) or True)
    return messages


def _jobs() -> dict[str, Job]:
    with get_session() as s:
        return {j.external_id: j for j in s.exec(select(Job)).all()}


def test_search_urls():
    site = LinkedInSite(remote_country="India")
    q = parse_qs(urlparse(site.search_url("AI engineer", "Remote", 1)).query)
    assert q == {"keywords": ["AI engineer"], "f_TPR": ["r604800"], "location": ["India"],
                 "f_WT": ["2"], "start": ["25"]}
    assert "location=Delhi%2C+India" in site.search_url("AI engineer", "Delhi, India", 0)


def test_browse_reads_cards_descriptions_and_stores_jobs(fresh_db, tmp_path, no_wait):
    sessions = Sessions(tmp_path)
    config = BrowseConfig(queries=["AI engineer"], locations=["Delhi, India", "Bengaluru, India", "Remote"],
                          pages=2, details=3)

    report = browse_site(LinkedInSite(), config, session_factory=sessions, pace=no_wait)

    jobs = _jobs()
    assert set(jobs) == {"1001", "1002", "2001", "2002", "300"}
    assert (report.pages, report.stored, report.descriptions) == (6, 5, 3)  # 3 searches × (p1 + empty p2)
    # Read-only: only search URLs were ever opened.
    assert all(u.startswith("https://www.linkedin.com/jobs/search/") for u in sessions.visited)
    delhi = jobs["1001"]
    assert delhi.source == "linkedin" and delhi.url == "https://www.linkedin.com/jobs/view/1001/"
    assert delhi.description_raw == "Build LLM systems with Python. Job 1001"  # read from the details pane
    assert jobs["2002"].title_norm == "senior machine learning engineer"  # normalized like any source
    assert jobs["300"].remote is True and jobs["300"].location_norm == "remote"
    with get_session() as s:
        assert s.get(SourceHealth, "linkedin:browse").success_count == 1  # type: ignore[union-attr]


def test_details_budget_skips_jobs_already_described(fresh_db, tmp_path, no_wait):
    config = BrowseConfig(locations=["Delhi, India"], pages=1, details=5)
    browse_site(LinkedInSite(), config, session_factory=Sessions(tmp_path), pace=no_wait)
    again = browse_site(LinkedInSite(), config, session_factory=Sessions(tmp_path), pace=no_wait, force=True)
    assert again.descriptions == 0 and again.stored == 2


def test_once_a_day_unless_forced(fresh_db, tmp_path, no_wait):
    config = BrowseConfig(locations=["Delhi, India"], pages=1)
    browse_site(LinkedInSite(), config, session_factory=Sessions(tmp_path), pace=no_wait)
    second = browse_site(LinkedInSite(), config, session_factory=Sessions(tmp_path), pace=no_wait)
    assert second.skipped and second.pages == 0


def test_checkpoint_stops_snapshots_and_notifies(fresh_db, tmp_path, no_wait, sent):
    sessions = Sessions(tmp_path, route=lambda url: CHECKPOINT)
    report = browse_site(LinkedInSite(), BrowseConfig(), session_factory=sessions, pace=no_wait)

    assert report.stopped and report.stored == 0
    assert len(sessions.visited) == 1  # stopped at the first page; nothing retried
    with get_session() as s:
        failure = s.exec(select(FailureEvent)).one()
        health = s.get(SourceHealth, "linkedin:browse")
    assert (failure.phase, failure.failure_class) == ("browse_checkpoint", FailureClass.AUTH_WALL)
    assert failure.snapshot_id and health is not None and health.failure_count == 1
    assert "browse-login linkedin" in sent[0]


def test_visible_challenge_is_a_checkpoint_hidden_sensor_is_not(tmp_path):
    site = LinkedInSite()
    visible = _page([]).replace("width:0;height:0;border:0", "width:400px;height:400px") \
                       .replace("li.protechts.net/index.html", "www.linkedin.com/captcha/challenge")
    with Sessions(tmp_path)(False) as session:
        session.page.set_content(_page([("1", "AI Engineer", "Acme", "Delhi")]))
        assert site.checkpoint(session.page) is None
        session.page.set_content(visible)
        assert site.checkpoint(session.page) == "verification challenge on page"


def test_fallback_reads_public_slug_links(fresh_db, tmp_path, no_wait):
    # Public-page shape: the slug link, then title / company / location blocks.
    public = ("<ul><li><a href='https://in.linkedin.com/jobs/view/ai-ml-developer-at-infosys-4475427817?x'>"
              "<span>AI/ML Developer</span></a><h4>Infosys</h4><div>Bengaluru</div></li></ul>")
    sessions = Sessions(tmp_path, route=lambda url: public if "start" not in url else "<ul></ul>")
    browse_site(LinkedInSite(), BrowseConfig(locations=["Bengaluru, India"], pages=2),
                session_factory=sessions, pace=no_wait)
    job = _jobs()["4475427817"]
    assert (job.title, job.company) == ("AI/ML Developer", "Infosys")


def test_kill_switch_stops_browsing(fresh_db, tmp_path, no_wait):
    kill_file().write_text("x")
    try:
        report = browse_site(LinkedInSite(), BrowseConfig(), session_factory=Sessions(tmp_path), pace=no_wait)
    finally:
        kill_file().unlink()
    assert report.stopped == "kill switch" and report.pages == 0


def test_browse_needs_explicit_opt_in(fresh_db):
    result = CliRunner().invoke(scraper_cli, ["browse"])
    assert result.exit_code == 2 and "--ok" in result.output
    assert CliRunner().invoke(scraper_cli, ["browse", "naukri", "--ok"]).exit_code == 1  # not in the cluster yet


def test_login_waits_for_the_human(tmp_path):
    states = iter([False, False, True])
    site = LinkedInSite()
    site.is_logged_in = lambda page: next(states)  # type: ignore[method-assign]
    polls: list[float] = []
    assert browse_login(site, session_factory=Sessions(tmp_path, route=lambda u: "<p>login</p>"),
                        poll=polls.append) is True
    assert polls == [2.0, 2.0]

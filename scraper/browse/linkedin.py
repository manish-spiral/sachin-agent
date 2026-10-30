"""LinkedIn job search, read with the user's own logged-in session."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from scraper.browse.base import BrowseSite

if TYPE_CHECKING:
    from playwright.sync_api import Page

RESULTS_PER_PAGE = 25
PAST_WEEK = "r604800"
REMOTE = "2"  # LinkedIn workplace-type filter value for remote
_CHECKPOINT_URL = re.compile(r"/(checkpoint|authwall|login|uas/login|signup)", re.I)
_CHECKPOINT_TEXT = re.compile(
    r"security (check|verification)|let.s do a quick security check|verify (that )?you.re (a )?human|"
    r"unusual activity|sign in to (view|continue)|join linkedin", re.I)


class LinkedInSite(BrowseSite):
    name = "linkedin"
    domain = "linkedin.com"
    login_url = "https://www.linkedin.com/login"

    def search_url(self, query: str, location: str, page: int) -> str:
        params = {"keywords": query, "f_TPR": PAST_WEEK}
        if location.strip().lower() == "remote":
            # Remote roles open to candidates in the user's country.
            params |= {"location": self.remote_country, "f_WT": REMOTE}
        else:
            params["location"] = location
        if page:
            params["start"] = str(page * RESULTS_PER_PAGE)
        return f"https://www.linkedin.com/jobs/search/?{urlencode(params)}"

    def checkpoint(self, page: Page) -> str | None:
        if _CHECKPOINT_URL.search(page.url):
            return f"redirected to {page.url}"
        # LinkedIn embeds a hidden bot-detection sensor iframe on every page;
        # only a challenge the human would actually see is a checkpoint.
        for frame in page.locator("iframe[src*='captcha'], iframe[title*='challenge' i], iframe[src*='challenge']").all():
            box = frame.bounding_box() if frame.is_visible() else None
            if box and box["width"] > 100 and box["height"] > 100:
                return "verification challenge on page"
        head = page.locator("main, body").first.inner_text(timeout=5_000)[:2000]
        if _CHECKPOINT_TEXT.search(head) and not page.locator(self.spec.card).count():
            return "LinkedIn asks for verification or a login"
        return None

    def is_logged_in(self, page: Page) -> bool:
        return bool(page.locator("img.global-nav__me-photo, .global-nav__me, [data-control-name='nav.settings']").count())

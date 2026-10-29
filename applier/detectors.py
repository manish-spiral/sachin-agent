"""Detect pages the agent must not get past on its own (constraint 8).

CAPTCHAs and login walls always go to the human. Consent banners go to the
human unless the human already decided for this domain, in which case that
exact decision is replayed. Nothing here ever solves or accepts anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shared.schemas.application import PauseReason

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

# Challenge widgets that need a human. The invisible reCAPTCHA many forms
# embed (an "anchor" iframe with size=invisible) is not a wall.
_CAPTCHA_FRAMES = (
    "iframe[src*='recaptcha/api2/bframe']",
    "iframe[src*='recaptcha/enterprise/bframe']",
    "iframe[src*='hcaptcha.com'][src*='challenge']",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='challenge' i]",
)
_CAPTCHA_ANCHOR = "iframe[src*='recaptcha'][src*='anchor']:not([src*='size=invisible'])"
_CAPTCHA_TEXT = re.compile(r"verify (that )?you are (a )?human|are you a robot|complete the captcha", re.I)
_LOGIN_TEXT = re.compile(r"\b(sign|log) ?in to (continue|apply)|please (sign|log) ?in\b", re.I)
_LOGIN_URL = re.compile(r"/(login|signin|sign-in|sso|auth)(\b|/|\?)", re.I)
_CONSENT_CONTAINERS = (
    "#onetrust-banner-sdk",
    "#CybotCookiebotDialog",
    "[id*='cookie' i][role='dialog']",
    "[class*='cookie' i][role='dialog']",
    "[aria-label*='cookie' i]",
    "[id*='consent' i]:not(input):not(select):not(textarea)",
)
_CONSENT_BUTTON = re.compile(r"accept|agree|allow|reject|decline|deny|necessary only|essential", re.I)


@dataclass(frozen=True)
class Blocker:
    reason: PauseReason
    detail: str
    # For consent banners: the choices on offer, verbatim.
    options: tuple[str, ...] = ()


def _visible(locator: Locator) -> bool:
    return any(item.is_visible() for item in locator.all())


def detect_captcha(page: Page) -> Blocker | None:
    for selector in _CAPTCHA_FRAMES:
        if _visible(page.locator(selector)):
            return Blocker(PauseReason.CAPTCHA, f"challenge widget visible ({selector})")
    if _visible(page.locator(_CAPTCHA_ANCHOR)):
        return Blocker(PauseReason.CAPTCHA, "checkbox CAPTCHA visible")
    if _CAPTCHA_TEXT.search(page.locator("body").inner_text()):
        return Blocker(PauseReason.CAPTCHA, "page asks to verify you are human")
    return None


def detect_login_wall(page: Page) -> Blocker | None:
    password = page.locator("input[type='password']")
    if _visible(password):
        return Blocker(PauseReason.LOGIN, "password field visible")
    if _LOGIN_URL.search(page.url) and _visible(page.locator("form")):
        return Blocker(PauseReason.LOGIN, f"login URL {page.url}")
    if _LOGIN_TEXT.search(page.locator("body").inner_text()):
        return Blocker(PauseReason.LOGIN, "page asks you to sign in")
    return None


def consent_banner(page: Page) -> Locator | None:
    for selector in _CONSENT_CONTAINERS:
        banner = page.locator(selector)
        for item in banner.all():
            if item.is_visible() and _consent_buttons(item):
                return item
    return None


def _consent_buttons(banner: Locator) -> list[str]:
    texts = [t.strip() for t in banner.locator("button, [role='button'], a").all_inner_texts()]
    return [t for t in texts if t and _CONSENT_BUTTON.search(t)]


def detect_consent(page: Page) -> Blocker | None:
    banner = consent_banner(page)
    if banner is None:
        return None
    prompt = re.sub(r"\s+", " ", banner.inner_text()).strip()[:300]
    return Blocker(PauseReason.CONSENT, prompt, tuple(_consent_buttons(banner)))


def click_consent(page: Page, choice: str) -> bool:
    """Replay a human's banner decision: click the button with exactly that text."""
    banner = consent_banner(page)
    if banner is None:
        return False
    button = banner.get_by_role("button", name=choice, exact=True)
    if not button.count():
        button = banner.get_by_text(choice, exact=True)
    if not button.count():
        return False
    button.first.click()
    return True


def detect_blocker(page: Page) -> Blocker | None:
    """CAPTCHA first: it can sit on top of anything else."""
    return detect_captcha(page) or detect_login_wall(page) or detect_consent(page)

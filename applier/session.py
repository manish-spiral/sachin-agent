"""BrowserSession: a Playwright persistent context with the primitives adapters use.

Persistent so cookies and logins survive between runs (a login solved by the
human once stays solved). Playwright is imported lazily.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Locator, Page, Playwright

NAVIGATION_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 10_000


class BrowserSession:
    def __init__(self, profile_dir: Path, *, headless: bool = True) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self._playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> BrowserSession:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> BrowserSession:
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir), headless=self.headless, accept_downloads=False
        )
        self.context.set_default_timeout(ACTION_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        self._page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self.context = self._playwright = self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("BrowserSession not started")
        return self._page

    def goto(self, url: str) -> None:
        logger.info(f"goto {url}")
        self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass  # long-polling pages never go idle; the DOM is what we need

    def find_by_role(self, role: Any, name: str | re.Pattern[str]) -> Locator:
        return self.page.get_by_role(role, name=name)

    def fill_by_label(self, label: str | re.Pattern[str], value: str) -> None:
        self.page.get_by_label(label).first.fill(value)

    def upload(self, locator: Locator, path: Path) -> None:
        locator.set_input_files(str(path))

    def pause_for_human(self, reason: str, *, interactive: bool) -> bool:
        """Hand control to the human. Returns True once they say they are done;
        False when nobody is watching (the caller must persist and stop)."""
        if not interactive or self.headless:
            return False
        input(f"\n⏸  {reason}\n   Act in the browser window, then press Enter to continue… ")
        return True

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)
        return path

    def dom(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.page.content(), encoding="utf-8")
        return path

    def save_state(self, path: Path) -> Path:
        """Cookies + localStorage, so a paused application can resume elsewhere."""
        assert self.context is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(path))
        return path

    def restore_state(self, path: Path) -> None:
        assert self.context is not None
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("cookies"):
                self.context.add_cookies(state["cookies"])

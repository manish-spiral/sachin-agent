"""HTML → PDF via headless Chromium, and an ATS read-back check."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

MAX_PAGES = 2

_env = Environment(
    loader=PackageLoader("matcher", "templates"),
    autoescape=select_autoescape(["html", "j2"]),
    keep_trailing_newline=True,
)


def render_html(template: str, **context: Any) -> str:
    return _env.get_template(template).render(**context)


def html_to_pdf(html: str, path: Path) -> None:
    from playwright.sync_api import sync_playwright

    path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            # No network: the template is self-contained, and a resume render
            # must never fetch anything.
            page.route("**/*", lambda route: route.abort())
            page.set_content(html, wait_until="load")
            page.pdf(path=str(path), prefer_css_page_size=True, print_background=False)
        finally:
            browser.close()


def _squash(text: str) -> str:
    # A hyphenated word wrapped at its hyphen extracts as "shipment- tracking";
    # parsers rejoin it, and so must the check.
    text = re.sub(r"-\s+", "-", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def ats_check(pdf_path: Path, expected_lines: list[str]) -> list[str]:
    """What a resume parser would trip on: text it can't read back in order,
    images, or too many pages. Empty list = passes."""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    problems = []
    if len(reader.pages) > MAX_PAGES:
        problems.append(f"{len(reader.pages)} pages (max {MAX_PAGES})")
    if any(page.images for page in reader.pages):
        problems.append("contains images")

    text = _squash(" ".join(page.extract_text() or "" for page in reader.pages))
    cursor = 0
    for line in expected_lines:
        needle = _squash(line)
        if not needle:
            continue
        found = text.find(needle, cursor)
        if found == -1:
            where = "missing" if needle not in text else "out of order"
            problems.append(f"{where} in extracted text: {line[:80]!r}")
        else:
            cursor = found + len(needle)
    return problems

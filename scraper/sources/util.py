"""Small helpers shared by source adapters."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(value: str | None) -> str:
    """HTML → plain text. Good enough for matching; Phase 4 may improve."""
    if not value:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", value))
    return _WS_RE.sub(" ", text).strip()


def parse_dt(value: str | int | float | None) -> datetime | None:
    """Parse ISO-8601, RFC-2822, or epoch (s or ms) dates. Returns None on failure."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        # Lever sends epoch milliseconds; anything past year ~5138 in seconds is ms.
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        # handles "2024-01-01T00:00:00Z"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        # handles "Mon, 01 Jan 2024 00:00:00 GMT"
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except (TypeError, ValueError):
        return None
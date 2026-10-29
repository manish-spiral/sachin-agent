"""Telegram notifications for pauses, so the human doesn't have to watch.

Unconfigured (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) means log-only. A
notification failure is logged and never breaks the run that triggered it.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from loguru import logger

from shared.config import get_settings

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT_SECONDS = 15.0
MAX_CAPTION = 1000


def notify(text: str, *, photo: Path | None = None, transport: httpx.BaseTransport | None = None) -> bool:
    """Send `text` (with an optional screenshot). Returns True if delivered."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.info(f"notify (Telegram not configured): {text}")
        return False
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            if photo is not None and photo.exists():
                response = client.post(
                    API.format(token=settings.telegram_bot_token, method="sendPhoto"),
                    data={"chat_id": settings.telegram_chat_id, "caption": text[:MAX_CAPTION]},
                    files={"photo": (photo.name, photo.read_bytes(), "image/png")},
                )
            else:
                response = client.post(
                    API.format(token=settings.telegram_bot_token, method="sendMessage"),
                    json={"chat_id": settings.telegram_chat_id, "text": text},
                )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        # The token is part of the URL; log the error type only, never the URL.
        logger.warning(f"Telegram notification failed: {type(exc).__name__}")
        return False
    return True

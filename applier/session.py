"""BrowserSession — real implementation in Phase 7. Stub for Phase 0."""

from __future__ import annotations

from loguru import logger


class BrowserSession:
    def __init__(self, profile_dir: str | None = None) -> None:
        self.profile_dir = profile_dir
        logger.info(f"BrowserSession stub (profile={profile_dir})")
"""Centralized loguru setup. Call `configure_logging(<module>)` from every CLI."""

from __future__ import annotations

import sys

from loguru import logger

from shared.config import get_settings
from shared.paths import ensure_dirs


def configure_logging(module_name: str = "jobagent") -> None:
    settings = get_settings()
    ensure_dirs()

    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.add(
        settings.logs_dir / f"{module_name}.log",
        level=settings.log_level,
        rotation="10 MB",
        retention=5,
        enqueue=True,
    )
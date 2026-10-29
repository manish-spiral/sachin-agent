"""Load per-source company board slugs from JSON."""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

DEFAULT_PATH = Path("scraper/companies.json")


def load_companies(source: str, path: Path = DEFAULT_PATH) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"could not read {path}: {exc}")
        return []
    values = data.get(source, [])
    if not isinstance(values, list):
        logger.warning(f"{path}: {source} is not a list; ignoring")
        return []
    return [str(v).strip() for v in values if str(v).strip()]
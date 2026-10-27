"""v1 migration shim — create_all / drop_all. Replace with Alembic in Phase 2."""

from __future__ import annotations

from pathlib import Path

from sqlmodel import SQLModel

from shared.config import get_settings
from shared.db import models  # noqa: F401  (registers tables)
from shared.db.engine import get_engine, reset_engine


def _ensure_sqlite_dir() -> None:
    s = get_settings()
    if s.database_url.startswith("sqlite:///"):
        db_path = Path(s.database_url.replace("sqlite:///", "", 1))
        db_path.parent.mkdir(parents=True, exist_ok=True)


def fast_create() -> None:
    """create_all — used by tests only. Production: `sachin-agent-db init`."""
    _ensure_sqlite_dir()
    SQLModel.metadata.create_all(get_engine())


def drop_db() -> None:
    s = get_settings()
    if not s.database_url.startswith("sqlite"):
        raise RuntimeError("Refusing to drop a non-SQLite database")
    reset_engine()  # ensure clean metadata binding
    SQLModel.metadata.drop_all(get_engine())
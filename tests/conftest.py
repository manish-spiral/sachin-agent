"""Pytest fixtures — isolated temp DB per session."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def _test_env():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/test.db"
        os.environ["DATA_DIR"] = tmp
        os.environ["LOGS_DIR"] = tmp
        os.environ["SNAPSHOT_DIR"] = tmp
        os.environ["PROFILE_DATA_DIR"] = tmp
        os.environ["BROWSER_PROFILE_DIR"] = tmp

        # Clear caches populated before env was set.
        from shared.config import reset_settings
        from shared.db.engine import reset_engine

        reset_settings()
        reset_engine()
        yield


@pytest.fixture
def fresh_db(_test_env):
    from shared.db.engine import reset_engine
    from shared.db.migrate import drop_db, fast_create

    reset_engine()
    drop_db()
    fast_create()
    yield
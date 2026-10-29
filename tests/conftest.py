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
        # Tests must never reach a real LLM or Telegram, whatever .env holds.
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
                    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ[key] = ""

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

@pytest.fixture
def applicant(fresh_db, tmp_path):
    """A finalized profile plus one approved job with tailored documents."""
    from shared.profile.store import save_profile
    from tests.applier_helpers import add_job, make_profile

    save_profile(make_profile(**{"Where did you first hear about this role?": "LinkedIn"}))
    add_job(tmp_path)
    return tmp_path

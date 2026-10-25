"""Single engine factory + session helper. Cached per process."""

from __future__ import annotations

from sqlmodel import Session, create_engine

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        from shared.config import get_settings

        s = get_settings()
        connect_args = {}
        if s.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        _engine = create_engine(s.database_url, echo=False, connect_args=connect_args)
    return _engine


def get_session() -> Session:
    return Session(get_engine(), expire_on_commit=False)


def reset_engine() -> None:
    """Testing hook."""
    global _engine
    _engine = None
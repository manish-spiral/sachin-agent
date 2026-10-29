"""Per-source health counters. Used by the self-improvement loop later."""

from __future__ import annotations

from datetime import UTC, datetime

from shared.db.engine import get_session
from shared.schemas.source_health import SourceHealth


def _touch(session, source: str) -> SourceHealth:
    health = session.get(SourceHealth, source)
    if health is None:
        health = SourceHealth(source=source)
        session.add(health)
    return health


def record_source_success(source: str) -> None:
    """Count one successful run (not jobs) so success/failure counts are comparable."""
    with get_session() as s:
        health = _touch(s, source)
        health.last_success = datetime.now(UTC)
        health.success_count += 1
        s.commit()


def record_source_failure(source: str, error: str) -> None:
    with get_session() as s:
        health = _touch(s, source)
        health.last_error = datetime.now(UTC)
        health.last_error_message = error[:4000]
        health.failure_count += 1
        s.commit()
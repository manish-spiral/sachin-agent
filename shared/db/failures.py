"""Emit FailureEvent rows. The self-improvement loop starts here."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlparse

from shared.db.engine import get_session
from shared.schemas.failure import FailureClass, FailureEvent


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).netloc.lower() or None


def emit_failure(
    *,
    source_module: str,
    phase: str,
    error: Exception,
    failure_class: FailureClass = FailureClass.UNKNOWN,
    domain: str | None = None,
    context: dict[str, Any] | None = None,
    job_id: str | None = None,
    application_id: str | None = None,
    snapshot_id: str | None = None,
    session=None,
) -> None:
    event = FailureEvent(
        id=str(uuid.uuid4()),
        source_module=source_module,
        domain=domain,
        phase=phase,
        job_id=job_id,
        application_id=application_id,
        snapshot_id=snapshot_id,
        error_type=type(error).__name__,
        error_message=str(error)[:4000],
        failure_class=failure_class,
        context=context or {},
    )
    if session is not None:
        session.add(event)
        session.commit()
        return
    with get_session() as s:
        s.add(event)
        s.commit()
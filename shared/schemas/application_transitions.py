"""Legal Application.status transitions. Every write goes through transition()."""

from __future__ import annotations

from datetime import UTC, datetime

from shared.schemas.application import Application, ApplicationStatus

S = ApplicationStatus
LEGAL_TRANSITIONS: dict[ApplicationStatus, set[ApplicationStatus]] = {
    S.PENDING: {S.PAUSED, S.SUBMITTED, S.FAILED, S.ABANDONED},
    S.PAUSED: {S.PENDING, S.SUBMITTED, S.FAILED, S.ABANDONED},  # PENDING = resumed
    S.FAILED: {S.PENDING, S.ABANDONED},  # retry or give up
    S.SUBMITTED: set(),  # terminal
    S.ABANDONED: set(),  # terminal
}


class IllegalApplicationTransition(ValueError):
    pass


def transition(app: Application, to: ApplicationStatus) -> Application:
    if app.status == to:
        return app
    allowed = LEGAL_TRANSITIONS[app.status]
    if to not in allowed:
        raise IllegalApplicationTransition(
            f"Application {app.id}: illegal transition {app.status.value} → {to.value}; "
            f"allowed: {sorted(s.value for s in allowed) or 'none (terminal)'}"
        )
    app.status = to
    if to != S.PAUSED:
        app.pause_reason = None
    app.updated_at = datetime.now(UTC)
    return app

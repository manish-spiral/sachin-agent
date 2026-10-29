"""Per-domain assertion suites: what "this adapter works" means offline."""

from __future__ import annotations

from applier.adapters.form import FormAdapter, locate
from sandbox.replay import Assertion, ReplaySession
from shared.schemas.snapshot import SnapshotIndex

# Snapshot intents that show an application form (not confirmations or walls).
FORM_INTENTS = ("open", "inspect", "filled", "refilled")


def form_detected(adapter: FormAdapter, session: ReplaySession, snapshot: SnapshotIndex) -> str | None:
    return None if adapter.detect(session) else f"form marker {adapter.spec.form_marker.value!r} not found"


def required_fields_resolve(adapter: FormAdapter, session: ReplaySession, snapshot: SnapshotIndex) -> str | None:
    missing = [f.intent for f in adapter.spec.fields
               if f.required and locate(session.page, f.locator).count() == 0]
    return f"required fields not found: {missing}" if missing else None


def submit_present(adapter: FormAdapter, session: ReplaySession, snapshot: SnapshotIndex) -> str | None:
    return None if locate(session.page, adapter.spec.submit).count() else \
        f"submit {adapter.spec.submit.value!r} not found"


def questions_extracted(adapter: FormAdapter, session: ReplaySession, snapshot: SnapshotIndex) -> str | None:
    if adapter.spec.questions is None:
        return None
    info = adapter.extract(session)
    return None if info.questions else "no custom questions extracted"


ATS_FORM_SUITE: list[Assertion] = [form_detected, required_fields_resolve, submit_present, questions_extracted]

# Domain suffix → suite. Every ATS form adapter is held to the same contract.
SUITES: dict[str, list[Assertion]] = {"greenhouse.io": ATS_FORM_SUITE}


def suite_for(domain: str) -> list[Assertion]:
    for suffix, suite in SUITES.items():
        if domain == suffix or domain.endswith("." + suffix):
            return suite
    return ATS_FORM_SUITE

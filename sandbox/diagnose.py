"""Classify every FailureEvent into the taxonomy — the loop starts to *see*.

Deterministic rules settle the obvious cases for free and with high
confidence; the LLM only sees what the rules can't place. The diagnosed class
replaces the emitter's provisional guess (kept in `diagnosis.emitted_class`).
No repair happens here: Phase 11 measures, Phase 13 changes.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlmodel import col, select

from shared.db.engine import get_session
from shared.llm import StructuredLLM, Task
from shared.schemas.failure import FailureClass, FailureEvent
from shared.schemas.snapshot import SnapshotIndex

PROMPT_VERSION = "diagnose_v1"
SNAPSHOT_EXCERPT_CHARS = 1500

F = FailureClass


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failure_class: FailureClass
    confidence: float
    evidence: str


@dataclass(frozen=True)
class Rule:
    name: str
    matches: Callable[[FailureEvent], bool]
    failure_class: FailureClass
    confidence: float


def _msg(pattern: str) -> Callable[[FailureEvent], bool]:
    regex = re.compile(pattern, re.I)
    return lambda e: bool(regex.search(f"{e.error_type} {e.error_message}"))


# First match wins, so specific rules come before general ones.
RULES: list[Rule] = [
    Rule("pause_captcha", lambda e: e.phase == "pause_captcha", F.CAPTCHA, 1.0),
    Rule("pause_login", lambda e: e.phase == "pause_login", F.AUTH_WALL, 1.0),
    Rule("pause_consent", lambda e: e.phase == "pause_consent", F.CONSENT, 1.0),
    Rule("pause_unknown_field", lambda e: e.phase == "pause_unknown_field", F.UNKNOWN_FIELD, 1.0),
    Rule("rate_limit", _msg(r"\b429\b|rate.?limit|tokens per day|too many requests"), F.RATE_LIMIT, 0.95),
    Rule("selector", _msg(r"SelectorNotFound|matched nothing"), F.SELECTOR_DRIFT, 0.95),
    Rule("no_adapter", _msg(r"no adapter for"), F.UNSEEN_SITE, 0.95),
    Rule("llm_output", _msg(r"json_validate_failed|failed to (validate|generate) json|no structured output|"
                            r"max completion tokens"), F.SEMANTIC_MISMATCH, 0.9),
    Rule("auth", _msg(r"\b40[13]\b|AuthenticationError|unauthori[sz]ed|forbidden"), F.AUTH_WALL, 0.85),
    Rule("transport", _msg(r"\b50[0234]\b|timeout|timed out|ConnectError|transport error|connection reset"),
         F.TRANSIENT, 0.85),
    Rule("schema", lambda e: e.phase == "normalize" and bool(_msg(r"KeyError|TypeError|ValidationError|"
                                                                  r"AttributeError")(e)), F.SCHEMA_DRIFT, 0.85),
    # A board/endpoint that 404s is gone or moved: the source no longer exists as configured.
    Rule("not_found", lambda e: e.phase == "fetch" and bool(_msg(r"\b404\b")(e)), F.UNSEEN_SITE, 0.7),
]

SYSTEM_PROMPT = f"""\
You classify one failure of a job-application agent into exactly one class:
{", ".join(c.value for c in FailureClass)}.

- transient: network/server hiccup that would pass on retry.
- selector_drift: page loaded but an element the adapter expected is missing/renamed.
- schema_drift: an API/data shape changed (missing or renamed fields).
- rate_limit, auth_wall (login/permissions), captcha, consent: as named.
- unseen_site: no adapter/source exists for this site or endpoint.
- unknown_field: a form question the agent could not answer.
- novel_interaction: the page needs an interaction the adapter doesn't do
  (multi-step wizard, drag-and-drop, custom widget).
- semantic_mismatch: things "worked" but the result is wrong (bad LLM output,
  no confirmation after submit, wrong page).
- unknown: only if nothing fits.

evidence: one sentence quoting what in the failure supports the class.
confidence: 0–1.
"""


def rule_diagnosis(event: FailureEvent) -> tuple[Diagnosis, str] | None:
    for rule in RULES:
        if rule.matches(event):
            evidence = f"rule {rule.name}: {event.error_type}: {event.error_message[:160]}"
            return Diagnosis(failure_class=rule.failure_class, confidence=rule.confidence, evidence=evidence), rule.name
    return None


def _snapshot_excerpt(snapshot_id: str | None) -> str:
    if not snapshot_id:
        return "none"
    with get_session() as s:
        snap = s.get(SnapshotIndex, snapshot_id)
    if snap is None:
        return "missing"
    excerpt = f"url: {snap.url}\nintent: {snap.intent}\nsignature: {snap.page_signature}"
    if snap.a11y_path and Path(snap.a11y_path).exists():
        excerpt += "\naria:\n" + Path(snap.a11y_path).read_text(encoding="utf-8")[:SNAPSHOT_EXCERPT_CHARS]
    return excerpt


def llm_diagnosis(event: FailureEvent, llm: StructuredLLM) -> Diagnosis:
    user = (
        f"module: {event.source_module}\nphase: {event.phase}\ndomain: {event.domain}\n"
        f"error_type: {event.error_type}\nerror_message: {event.error_message[:1500]}\n"
        f"context: {json.dumps(event.context, default=str)[:800]}\n"
        f"snapshot:\n{_snapshot_excerpt(event.snapshot_id)}"
    )
    return llm(Task.DIAGNOSE, SYSTEM_PROMPT, user, Diagnosis)


@dataclass
class DiagnoseReport:
    by_method: Counter[str] = field(default_factory=Counter)
    by_class: Counter[str] = field(default_factory=Counter)
    reclassified: int = 0
    left: int = 0


def diagnose_pending(*, llm: StructuredLLM | None, limit: int | None = None,
                     redo: bool = False) -> DiagnoseReport:
    """Diagnose undiagnosed failures (all of them with `redo`). Without an LLM,
    events no rule matches stay undiagnosed for a later run."""
    query = select(FailureEvent).order_by(col(FailureEvent.created_at))
    if not redo:
        query = query.where(col(FailureEvent.diagnosed_at).is_(None))
    report = DiagnoseReport()
    with get_session() as session:
        for event in session.exec(query.limit(limit)).all():
            # Rows older than the diagnosis column hold NULL, not {}.
            previous = event.diagnosis or {}
            emitted = previous.get("emitted_class") or (event.failure_class.value if event.failure_class else None)
            ruled = rule_diagnosis(event)
            if ruled is not None:
                diagnosis, method = ruled[0], f"rule:{ruled[1]}"
                extra: dict[str, str] = {}
            elif llm is not None:
                try:
                    diagnosis, method = llm_diagnosis(event, llm), "llm"
                    extra = {"prompt_version": PROMPT_VERSION, "model": llm.model_name(Task.DIAGNOSE)}
                except Exception as exc:
                    logger.warning(f"LLM diagnosis failed for {event.id}: {exc}")
                    report.left += 1
                    continue
            else:
                report.left += 1
                continue

            if emitted and emitted != diagnosis.failure_class.value:
                report.reclassified += 1
            event.failure_class = diagnosis.failure_class
            event.diagnosed_at = datetime.now(UTC)
            event.diagnosis = {"method": method, "confidence": diagnosis.confidence,
                               "evidence": diagnosis.evidence, "emitted_class": emitted, **extra}
            session.add(event)
            report.by_method[method.split(":")[0]] += 1
            report.by_class[diagnosis.failure_class.value] += 1
        session.commit()
    return report

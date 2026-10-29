"""L1 selector healing: propose → replay against snapshots → promote (or not).

Triggered by a SELECTOR_DRIFT failure (a locator matched nothing on a page
that loaded). Candidates come from the failing page itself — labels, ids and
roles that mean the same thing — plus optionally an LLM reading the ARIA tree.
Every candidate is judged offline by the replay harness; the live site is
never touched. A candidate is promoted only if it passes on the snapshots that
share the failing page's structure and beats the active version there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, ConfigDict
from rapidfuzz import fuzz
from sqlmodel import select

from applier.adapters import spec_for_url
from applier.adapters.form import FormAdapter, locate
from applier.adapters.spec import AdapterSpec, LocatorSpec, Strategy
from applier.adapters.versions import active_version, promote
from sandbox.harness import form_snapshots, run_suite
from sandbox.offline import OfflineBrowser
from shared.db.engine import get_session
from shared.llm import StructuredLLM, Task
from shared.schemas.adapter import AdapterVersion
from shared.schemas.failure import FailureClass, FailureEvent
from shared.schemas.snapshot import SnapshotIndex

PROMOTION_THRESHOLD = 0.9
MATCH_THRESHOLD = 85
PROMPT_VERSION = "heal_selector_v1"
# Label/role locators survive markup changes that break ids; prefer them on ties.
_STRATEGY_RANK = {Strategy.LABEL: 3, Strategy.ROLE: 2, Strategy.CSS: 1, Strategy.TEXT: 0}


class HealError(RuntimeError):
    pass


# --- reading the failing page --------------------------------------------------


@dataclass
class _Control:
    tag: str
    id: str = ""
    name: str = ""
    type: str = ""
    text: str = ""


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[_Control] = []
        self.labels: dict[str, str] = {}  # for-id -> label text
        self.buttons: list[str] = []
        self._label_for: str | None = None
        self._in_button = False
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag in {"input", "select", "textarea"}:
            self.controls.append(_Control(tag, a.get("id", ""), a.get("name", ""), a.get("type", "")))
            if a.get("type") == "submit" and a.get("value"):
                self.buttons.append(a["value"])
        elif tag == "label":
            self._label_for, self._text = a.get("for", ""), []
        elif tag == "button":
            self._in_button, self._text = True, []

    def handle_data(self, data: str) -> None:
        if self._label_for is not None or self._in_button:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        text = re.sub(r"\s+", " ", "".join(self._text)).strip()
        if tag == "label" and self._label_for is not None:
            if self._label_for and text:
                self.labels[self._label_for] = text
            self._label_for = None
        elif tag == "button" and self._in_button:
            if text:
                self.buttons.append(text)
            self._in_button = False


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _words(intent: str) -> str:
    return intent.replace("_", " ").replace("-", " ")


def deterministic_candidates(html: str, intent: str) -> list[LocatorSpec]:
    """Locators on the failing page that plausibly mean `intent`."""
    page = _PageParser()
    page.feed(html)
    out: list[LocatorSpec] = []
    if intent == "submit":
        # "Apply" usually *opens* the form; only fall back to it (or "Send")
        # when no button says "submit", or healing could click the wrong one.
        submits = [t for t in page.buttons if re.search(r"submit", t, re.I)]
        others = [t for t in page.buttons if re.search(r"apply|send", t, re.I)]
        return [LocatorSpec(strategy=Strategy.ROLE, role="button", value=t) for t in submits or others]

    # The form marker is the first required field, so heal it like that field.
    target = "first_name" if intent == "form" else intent
    for control in page.controls:
        for attr in ("id", "name"):
            value = getattr(control, attr)
            if value and fuzz.ratio(_norm(value), _norm(target)) >= MATCH_THRESHOLD:
                selector = f"{control.tag}#{value}" if attr == "id" else f'{control.tag}[name="{value}"]'
                out.append(LocatorSpec(strategy=Strategy.CSS, value=selector))
    for for_id, text in page.labels.items():
        clean = text.rstrip("*").strip()
        if fuzz.token_set_ratio(clean.lower(), _words(target)) >= MATCH_THRESHOLD:
            out.append(LocatorSpec(strategy=Strategy.LABEL, value=clean))
            if for_id:
                out.append(LocatorSpec(strategy=Strategy.CSS, value=f"#{for_id}"))
    return out


# --- LLM proposals ----------------------------------------------------------------


class ProposedLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Strategy
    value: str
    role: str | None
    rationale: str


class Proposals(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[ProposedLocator]


SYSTEM_PROMPT = """\
An automated form-filler's locator stopped matching after the site changed.
From the page's accessibility tree, propose 2-3 replacement locators for the
same element. Prefer what users see and is stable: label text (strategy
"label"), role + accessible name (strategy "role", role e.g. "button"), then
CSS. Only use names/ids that appear in the tree. Never target a different field.
"""


def llm_candidates(aria: str, intent: str, failing: LocatorSpec, llm: StructuredLLM) -> list[LocatorSpec]:
    user = (f"intent: {intent}\nfailing locator: {failing.model_dump(exclude_none=True)}\n\n"
            f"accessibility tree:\n{aria[:6000]}")
    try:
        result = llm(Task.HEAL, SYSTEM_PROMPT, user, Proposals)
    except Exception as exc:
        logger.warning(f"LLM proposals unavailable: {exc}")
        return []
    return [LocatorSpec(strategy=c.strategy, value=c.value, role=c.role) for c in result.candidates[:3]]


# --- evaluation and promotion -------------------------------------------------------


def _locator_of(spec: AdapterSpec, intent: str) -> LocatorSpec:
    if intent == "form":
        return spec.form_marker
    if intent == "submit":
        return spec.submit
    return next(f.locator for f in spec.fields if f.intent == intent)


def patch(spec: AdapterSpec, intent: str, locator: LocatorSpec) -> AdapterSpec:
    patched = spec.model_copy(deep=True)
    if intent == "form":
        patched.form_marker = locator
    elif intent == "submit":
        patched.submit = locator
    else:
        field_spec = next((f for f in patched.fields if f.intent == intent), None)
        if field_spec is None:
            raise HealError(f"{spec.domain} spec has no field {intent!r}")
        field_spec.locator = locator
    return patched


def _checked_intents(spec: AdapterSpec) -> list[str]:
    """Locators whose absence breaks the run: marker, required fields, submit."""
    return ["form", *(f.intent for f in spec.fields if f.required), "submit"]


def _resolve_rate(locator: LocatorSpec, snapshots: list[SnapshotIndex], browser: OfflineBrowser) -> float:
    hits = 0
    for snap in snapshots:
        with browser.page(snap) as page:
            hits += locate(page, locator).count() > 0
    return hits / len(snapshots) if snapshots else 0.0


@dataclass
class Trial:
    intent: str
    locator: LocatorSpec
    origin: str  # dom | llm
    eval_rate: float
    overall_rate: float


@dataclass
class HealReport:
    failure_id: str
    broken: list[str]
    baseline_rate: float
    trials: list[Trial] = field(default_factory=list)
    final_rate: float = 0.0
    promoted: AdapterVersion | None = None
    reason: str = ""


def heal(failure_id: str, *, llm: StructuredLLM | None = None,
         threshold: float = PROMOTION_THRESHOLD) -> HealReport:
    with get_session() as s:
        event = s.get(FailureEvent, failure_id)
        snapshot = s.get(SnapshotIndex, event.snapshot_id) if event and event.snapshot_id else None
    if event is None or event.failure_class != FailureClass.SELECTOR_DRIFT:
        raise HealError(f"{failure_id} is not a selector-drift failure")
    if snapshot is None:
        raise HealError(f"{failure_id} has no snapshot to heal against")

    builtin = spec_for_url(snapshot.url)
    if builtin is None:
        raise HealError(f"no adapter for {snapshot.url}")
    current = active_version(builtin)
    current_spec = AdapterSpec.load(Path(current.adapter_code_path))

    # Judge on the page as it is now (same structure as the failing page), and
    # prefer candidates that also still work on older captures of the domain.
    domain_snaps = form_snapshots(current_spec.domain)
    eval_set = [snapshot] + [x for x in domain_snaps
                             if x.page_signature == snapshot.page_signature and x.id != snapshot.id]
    overall_set = eval_set + [x for x in domain_snaps if x not in eval_set]
    baseline = run_suite(current_spec.domain, FormAdapter(current_spec), snapshots=eval_set).pass_rate

    html = Path(snapshot.html_path).read_text(encoding="utf-8")
    aria = Path(snapshot.a11y_path).read_text(encoding="utf-8") \
        if snapshot.a11y_path and Path(snapshot.a11y_path).exists() else ""

    with OfflineBrowser() as browser:
        with browser.page(snapshot) as page:
            broken = [i for i in _checked_intents(current_spec) if locate(page, _locator_of(current_spec, i)).count() == 0]
        report = HealReport(failure_id, broken, baseline)
        if not broken:
            report.reason = "every locator resolves on the failing page; not selector drift"
            return report

        healed = current_spec
        for intent in broken:
            failing = _locator_of(current_spec, intent)
            proposals = [(c, "dom") for c in deterministic_candidates(html, intent)]
            if llm is not None and aria:
                proposals += [(c, "llm") for c in llm_candidates(aria, intent, failing, llm)]
            seen: set[str] = set()
            trials = []
            for locator, origin in proposals:
                key = locator.model_dump_json()
                if key in seen or locator == failing:
                    continue
                seen.add(key)
                trials.append(Trial(intent, locator, origin, _resolve_rate(locator, eval_set, browser),
                                    _resolve_rate(locator, overall_set, browser)))
            report.trials += trials
            if not trials:
                report.reason = f"no candidates for {intent!r} on the failing page"
                return report
            best = max(trials, key=lambda t: (t.eval_rate, t.overall_rate, _STRATEGY_RANK[t.locator.strategy]))
            healed = patch(healed, intent, best.locator)

    report.final_rate = run_suite(current_spec.domain, FormAdapter(healed), snapshots=eval_set).pass_rate
    if report.final_rate < threshold or report.final_rate <= baseline:
        report.reason = (f"healed spec passes {report.final_rate:.0%} of current-structure snapshots "
                         f"(needs ≥{threshold:.0%} and > baseline {baseline:.0%}); not promoted")
        return report

    overall = run_suite(current_spec.domain, FormAdapter(healed), snapshots=overall_set).pass_rate
    changes = [f"{i}: {_locator_of(current_spec, i).value!r} → {_locator_of(healed, i).value!r}" for i in broken]
    report.promoted = promote(
        healed,
        parent=current,
        fix_summary="; ".join(changes),
        evidence={"failure_id": failure_id, "snapshot_id": snapshot.id, "baseline_rate": baseline,
                  "overall_rate": overall, "eval_snapshots": [x.id for x in eval_set],
                  "overall_snapshots": len(overall_set),
                  "trials": [{"intent": t.intent, "locator": t.locator.model_dump(exclude_none=True),
                              "origin": t.origin, "eval_rate": t.eval_rate, "overall_rate": t.overall_rate}
                             for t in report.trials],
                  "prompt_version": PROMPT_VERSION if llm else None},
        pass_rate=report.final_rate,
        source="synthesized",
    )
    report.reason = f"promoted v{report.promoted.version}"
    with get_session() as s:
        row = s.get(FailureEvent, failure_id)
        if row is not None:
            row.diagnosis = {**(row.diagnosis or {}), "healed_by": report.promoted.id}
            s.add(row)
            s.commit()
    return report


def pending_drift_failures() -> list[FailureEvent]:
    with get_session() as s:
        events = s.exec(select(FailureEvent).where(FailureEvent.failure_class == FailureClass.SELECTOR_DRIFT)).all()
    return [e for e in events if not (e.diagnosis or {}).get("healed_by") and e.snapshot_id]

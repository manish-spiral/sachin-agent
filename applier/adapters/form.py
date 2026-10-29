"""The one fixed executor that drives any AdapterSpec through the Adapter interface."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from loguru import logger

from applier.adapters.base import Adapter, FieldResult, FillReport, PageInfo
from applier.adapters.spec import AdapterSpec, FieldKind, FieldSpec, LocatorSpec, Strategy
from applier.context import Answer, ApplicantContext, Unanswerable
from applier.session import BrowserSession

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

OPTION_TIMEOUT_MS = 3_000


class SelectorNotFound(LookupError):
    """A spec locator matched nothing on a loaded page — selector drift."""

    def __init__(self, intent: str, locator: LocatorSpec) -> None:
        super().__init__(f"{intent}: {locator.strategy.value}={locator.value!r} matched nothing")
        self.intent = intent
        self.locator = locator


def locate(page: Page, spec: LocatorSpec) -> Locator:
    if spec.strategy == Strategy.CSS:
        return page.locator(spec.value)
    if spec.strategy == Strategy.LABEL:
        return page.get_by_label(spec.value)
    if spec.strategy == Strategy.ROLE:
        return page.get_by_role(spec.role or "button", name=spec.value)  # type: ignore[arg-type]
    return page.get_by_text(spec.value)


def _label_of(page: Page, control: Locator) -> str:
    """The question text for a control: its <label for>, aria-label, or labelledby."""
    text = control.evaluate(
        """el => {
            const byFor = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (byFor) return byFor.innerText;
            const by = el.getAttribute('aria-labelledby');
            if (by) return by.split(' ').map(i => document.getElementById(i)?.innerText || '').join(' ');
            return el.getAttribute('aria-label') || el.closest('label')?.innerText || '';
        }"""
    )
    return re.sub(r"\s+", " ", str(text)).strip().rstrip("*").strip()


def _is_required(control: Locator, label: str) -> bool:
    return bool(control.evaluate("el => el.required || el.getAttribute('aria-required') === 'true'")) \
        or label.endswith("*")


class FormAdapter(Adapter):
    def __init__(self, spec: AdapterSpec) -> None:
        self.spec = spec
        self.domain = spec.domain

    def form_url(self, posting_url: str) -> str:
        return self.spec.form_url.format(url=posting_url)

    def init(self, session: BrowserSession, url: str) -> None:
        session.goto(self.form_url(url))
        if self.spec.open_form is not None:
            opener = locate(session.page, self.spec.open_form)
            marker = locate(session.page, self.spec.form_marker)
            # Hidden forms are in the DOM but invisible: check visibility, not presence.
            if opener.count() and not (marker.count() and marker.first.is_visible()):
                opener.first.click()
                try:
                    marker.first.wait_for(state="visible", timeout=10_000)
                except Exception:
                    pass  # detect() reports a missing form as selector drift

    def detect(self, session: BrowserSession) -> bool:
        return locate(session.page, self.spec.form_marker).count() > 0

    def extract(self, session: BrowserSession) -> PageInfo:
        page = session.page
        questions = []
        if self.spec.questions is not None:
            questions = [_label_of(page, c) for c in locate(page, self.spec.questions).all()]
        return PageInfo(url=page.url, title=page.title(), questions=[q for q in questions if q])

    def _fill_one(self, page: Page, field: FieldSpec, applicant: ApplicantContext) -> FieldResult:
        control = locate(page, field.locator)
        if control.count() == 0:
            if field.required:
                raise SelectorNotFound(field.intent, field.locator)
            return FieldResult(field.intent, field.intent, "skipped", detail="not on this form")
        control = control.first

        if field.kind == FieldKind.FILE:
            path = applicant.resume_path if field.intent == "resume" else applicant.cover_letter_path
            if path is None:
                status = "missing" if field.required else "skipped"
                return FieldResult(field.intent, field.intent, status, detail="no document")
            control.set_input_files(str(path))
            return FieldResult(field.intent, field.intent, "filled", value=path.name)

        value = applicant.value_for(field.intent)
        if not value:
            status = "missing" if field.required else "skipped"
            return FieldResult(field.intent, field.intent, status, detail="no fact")
        self._type(page, control, field.kind, value)
        return FieldResult(field.intent, field.intent, "filled", value=value)

    def _options(self, page: Page, control: Locator) -> list[str]:
        if control.evaluate("el => el.tagName") == "SELECT":
            return [o.strip() for o in control.locator("option").all_inner_texts() if o.strip()]
        if control.get_attribute("role") != "combobox" and not control.get_attribute("aria-autocomplete"):
            return []
        control.click()
        listbox = page.get_by_role("option")
        try:
            listbox.first.wait_for(state="visible", timeout=OPTION_TIMEOUT_MS)
        except Exception:
            return []
        options = [o.strip() for o in listbox.all_inner_texts() if o.strip()]
        page.keyboard.press("Escape")
        return options

    def _type(self, page: Page, control: Locator, kind: FieldKind, value: str) -> None:
        tag = control.evaluate("el => el.tagName")
        if tag == "SELECT":
            control.select_option(label=value)
        elif kind == FieldKind.COMBOBOX or control.get_attribute("role") == "combobox":
            control.click()
            control.fill(value)
            page.get_by_role("option", name=value, exact=True).first.click()
        else:
            control.fill(value)

    def _fill_question(self, page: Page, control: Locator, applicant: ApplicantContext) -> FieldResult:
        label = _label_of(page, control)
        required = _is_required(control, label)
        if control.evaluate("el => el.tagName") != "SELECT" and control.input_value():
            return FieldResult("question", label, "skipped", detail="already answered")
        options = self._options(page, control)
        result = applicant.answer(label, options or None)
        if isinstance(result, Answer):
            self._type(page, control, FieldKind.COMBOBOX if options else FieldKind.TEXT, result.value)
            return FieldResult("question", label, "filled", value=result.value,
                               detail=f"{result.source.value}:{result.fact_id or '-'}")
        if result == Unanswerable.CONSENT:
            return FieldResult("question", label, "consent" if required else "skipped",
                               detail="legal acknowledgement: human decision")
        if result == Unanswerable.SKIP or not required:
            return FieldResult("question", label, "skipped", detail=result.value)
        return FieldResult("question", label, "unknown", detail=f"options: {options}" if options else "")

    def fill(self, session: BrowserSession, applicant: ApplicantContext) -> FillReport:
        page = session.page
        report = FillReport()
        for field in self.spec.fields:
            report.fields.append(self._fill_one(page, field, applicant))
        if self.spec.questions is not None:
            for control in locate(page, self.spec.questions).all():
                report.fields.append(self._fill_question(page, control, applicant))
        for f in report.fields:
            logger.debug(f"field {f.intent} {f.label!r}: {f.status} {f.detail}")
        return report

    def submit(self, session: BrowserSession) -> bool:
        page = session.page
        button = locate(page, self.spec.submit)
        if button.count() == 0:
            raise SelectorNotFound("submit", self.spec.submit)
        button.first.click()
        try:
            locate(page, self.spec.success).first.wait_for(state="visible", timeout=20_000)
        except Exception:
            return False
        return True

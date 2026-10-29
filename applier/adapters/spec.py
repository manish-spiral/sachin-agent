"""Declarative adapter specs: data, not code.

An adapter is a fixed executor (applier/adapters/form.py) plus one of these
specs. Healing (Phase 13) and synthesis (Phase 15) only ever produce specs, so
a self-modification can change *where* the agent clicks but never *what code*
runs — the safety core stays out of the mutable surface.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Strategy(StrEnum):
    CSS = "css"
    LABEL = "label"  # accessible name of a form control
    ROLE = "role"  # ARIA role + accessible name
    TEXT = "text"  # visible text


class LocatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Strategy
    value: str
    role: str | None = None  # only for Strategy.ROLE


class FieldKind(StrEnum):
    TEXT = "text"
    FILE = "file"
    COMBOBOX = "combobox"


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # What the field is for: an identity intent ("first_name", "email", ...),
    # "resume" / "cover_letter", or "question" (answered from the label text).
    intent: str
    locator: LocatorSpec
    kind: FieldKind = FieldKind.TEXT
    required: bool = False


class AdapterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str
    version: int = 1
    # Turns a job posting URL into the application form URL; {url} = posting.
    form_url: str = "{url}"
    # Clicked first if present (forms hidden behind an "Apply" button).
    open_form: LocatorSpec | None = None
    # The page is this adapter's form only if this matches.
    form_marker: LocatorSpec
    fields: list[FieldSpec]
    # Custom questions found at run time (ids vary per job).
    questions: LocatorSpec | None = None
    submit: LocatorSpec
    success: LocatorSpec

    @classmethod
    def load(cls, path: Path) -> AdapterSpec:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")

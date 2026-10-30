"""How the applier reaches a human: at the terminal now, or later via a pause.

`Absent` (the default for unattended runs) declines everything, so every
decision becomes a saved pause plus a notification. `Terminal` asks there and
then. Both are swapped for fakes in tests.
"""

from __future__ import annotations

from typing import Protocol

import typer

from applier.adapters.base import FillReport
from shared.browser.session import BrowserSession
from shared.schemas.application import Application, PauseReason


class Human(Protocol):
    def resolve(self, session: BrowserSession, reason: PauseReason, detail: str) -> bool:
        """Human clears a CAPTCHA / login in the browser now. True when done."""

    def choose(self, domain: str, prompt: str, options: list[str]) -> str | None:
        """A consent decision for `domain`, verbatim from `options`, or None."""

    def answer(self, question: str, options: list[str]) -> str | None:
        """An answer for a question no fact covers, or None."""

    def approve(self, application: Application, report: FillReport) -> bool:
        """Final go/no-go for submitting this filled form."""


class Absent:
    def resolve(self, session: BrowserSession, reason: PauseReason, detail: str) -> bool:
        return False

    def choose(self, domain: str, prompt: str, options: list[str]) -> str | None:
        return None

    def answer(self, question: str, options: list[str]) -> str | None:
        return None

    def approve(self, application: Application, report: FillReport) -> bool:
        return False


class Terminal:
    """Interactive mode for local runs. Needs a visible (headed) browser."""

    def resolve(self, session: BrowserSession, reason: PauseReason, detail: str) -> bool:
        return session.pause_for_human(f"{reason.value}: {detail}", interactive=True)

    def choose(self, domain: str, prompt: str, options: list[str]) -> str | None:
        typer.echo(f"\n{domain} asks for consent:\n  {prompt[:300]}")
        for i, option in enumerate(options, 1):
            typer.echo(f"  {i}. {option}")
        pick = typer.prompt("Your choice (number, blank = decide later)", default="", show_default=False)
        if pick.isdigit() and 1 <= int(pick) <= len(options):
            return options[int(pick) - 1]
        return None

    def answer(self, question: str, options: list[str]) -> str | None:
        hint = f" [{' / '.join(options)}]" if options else ""
        value = typer.prompt(f"\n{question}{hint}\nAnswer (blank = decide later)", default="", show_default=False)
        return value.strip() or None

    def approve(self, application: Application, report: FillReport) -> bool:
        typer.echo("\nFilled:")
        for f in report.fields:
            typer.echo(f"  {f.status:<8} {f.label[:60]:<60} {f.value[:40]}")
        return typer.confirm("Review the browser window. Submit this application?", default=False)

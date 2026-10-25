"""`jobagent-matcher` CLI."""

from __future__ import annotations

import typer

from matcher.scoring import score_pending_jobs
from shared.logging import configure_logging
from shared.paths import ensure_dirs

app = typer.Typer(help="Job matcher", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("matcher")
    ensure_dirs()


@app.command()
def run() -> None:
    """Score all new jobs."""
    score_pending_jobs()


if __name__ == "__main__":
    app()
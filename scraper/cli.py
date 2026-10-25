"""`jobagent-scraper` CLI."""

from __future__ import annotations

import asyncio

import typer

from scraper.runner import run_all_sources
from shared.logging import configure_logging
from shared.paths import ensure_dirs

app = typer.Typer(help="Job lead scraper", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("scraper")
    ensure_dirs()


@app.command()
def run() -> None:
    """Run all registered sources."""
    asyncio.run(run_all_sources())


if __name__ == "__main__":
    app()
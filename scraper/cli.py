"""`sachin-agent-scraper` CLI."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from scraper.runner import list_sources, run_all_sources
from shared.logging import configure_logging
from shared.paths import ensure_dirs

app = typer.Typer(help="Job lead scraper", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("scraper")
    ensure_dirs()


@app.command()
def sources() -> None:
    """List available sources."""
    for name in list_sources():
        typer.echo(f"  {name}")


@app.command()
def run(
    source: Annotated[
        str,
        typer.Option("--source", "-s", help="greenhouse | lever | ashby | all"),
    ] = "all",
    companies: Annotated[
        str | None,
        typer.Option(
            "--companies",
            "-c",
            help="Comma-separated board slugs. Overrides scraper/companies.json.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-l", help="Max jobs per company (debug)."),
    ] = None,
) -> None:
    """Fetch jobs from ATS APIs and upsert them."""
    valid = set(list_sources())
    if source == "all":
        selected = sorted(valid)
    elif source in valid:
        selected = [source]
    else:
        typer.echo(f"Unknown source: {source}. Valid: all, {', '.join(sorted(valid))}")
        raise typer.Exit(1)

    override = None
    if companies:
        override = [c.strip() for c in companies.split(",") if c.strip()]

    asyncio.run(
        run_all_sources(selected, companies_override=override, limit=limit)
    )


if __name__ == "__main__":
    app()
"""`sachin-agent-applier` CLI."""

from __future__ import annotations

import typer

from applier.session import BrowserSession
from shared.config import get_settings
from shared.logging import configure_logging
from shared.paths import ensure_dirs

app = typer.Typer(help="Job applier", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("applier")
    ensure_dirs()


@app.command()
def run() -> None:
    """Process approved applications."""
    s = get_settings()
    BrowserSession(profile_dir=str(s.browser_profile_dir))
    typer.echo("applier: stub — no applications processed yet")


if __name__ == "__main__":
    app()
"""`sachin-agent-profile` CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from shared.logging import configure_logging
from shared.paths import ensure_dirs, profile_md_path
from shared.profile.extract import extract_text
from shared.profile.facts import active_facts
from shared.profile.faithfulness import faithfulness_issues
from shared.profile.llm import ExtractionError, extract_profile_from_text
from shared.profile.render import render_markdown
from shared.profile.store import (
    list_history,
    load_current_profile,
    load_profile,
    restore_version,
    save_profile,
)
from shared.schemas.profile import Profile
from shared.schemas.profile_fact import FactKind

app = typer.Typer(help="Applicant profile management", no_args_is_help=True)


@app.callback()
def _bootstrap() -> None:
    configure_logging("profile")
    ensure_dirs()


@app.command()
def init() -> None:
    """Scaffold an empty profile.json."""
    if load_profile() is not None:
        typer.echo("profile.json already exists. Use `validate` or `render`.")
        raise typer.Exit(1)
    profile = Profile()
    save_profile(profile, source="init", notes="scaffold")
    typer.echo("Created profile-data/profile.json (draft).")
    typer.echo("Next: `sachin-agent-profile import <cv.pdf>` or edit the JSON manually.")


@app.command("import")
def import_cv(
    path: Annotated[Path, typer.Argument(exists=True, readable=True)],
    force: Annotated[
        bool, typer.Option("--force", help="Replace the existing profile (kept in history).")
    ] = False,
) -> None:
    """Extract a profile from a CV (PDF/DOCX/TXT) using the LLM."""
    if load_profile() is not None and not force:
        typer.echo(
            "✗ A profile already exists; importing would replace it, including hand edits "
            "(preferences, answers, finalized). Re-run with --force to replace it; the "
            "current version stays in `history` and can be restored.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        text = extract_text(path)
    except ValueError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from e

    try:
        profile = extract_profile_from_text(text)
    except ExtractionError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from e

    save_profile(profile, source="import", notes=f"from {path.name}", source_text=text)
    issues = profile.completeness_issues()
    typer.echo(f"Imported draft from {path.name} ({len(text)} chars).")
    suspicious = faithfulness_issues(profile, text)
    if suspicious:
        typer.echo("Check these against your CV before finalizing:")
        for item in suspicious:
            typer.echo(f"  - {item}")
    if issues:
        typer.echo("Draft is incomplete. Run `validate` to see what's missing:")
        for i in issues:
            typer.echo(f"  - {i}")
    else:
        typer.echo("Draft is complete. Review it, then set `finalized: true` in the JSON.")


@app.command()
def validate() -> None:
    """Check the profile for completeness and schema errors."""
    try:
        profile = load_profile()
    except ValidationError as e:
        typer.echo(f"✗ Schema errors:\n{e}", err=True)
        raise typer.Exit(1) from e

    if profile is None:
        typer.echo("No profile found. Run `init` first.")
        raise typer.Exit(1) 

    issues = profile.completeness_issues()
    if not issues and profile.finalized:
        typer.echo("✅ Profile is valid and finalized.")
        return
    if not issues:
        typer.echo("✅ Profile is complete but not finalized. Set `finalized: true`.")
        return

    typer.echo(f"⚠️  {len(issues)} issue(s):")
    for i in issues:
        typer.echo(f"  - {i}")
    raise typer.Exit(1) 


@app.command()
def render() -> None:
    """Write profile.md from the current JSON."""
    profile = load_profile()
    if profile is None:
        typer.echo("No profile found. Run `init` first.")
        raise typer.Exit(1)
    md = render_markdown(profile)
    out = profile_md_path()
    out.write_text(md, encoding="utf-8")
    typer.echo(f"Wrote {out}")


@app.command()
def facts(
    kind: Annotated[FactKind | None, typer.Option("--kind", "-k", help="Only this kind.")] = None,
    evidence: Annotated[bool, typer.Option("--evidence", help="Show the source CV line.")] = False,
) -> None:
    """List the citable facts every LLM must ground its claims in."""
    current = load_current_profile()
    if current is None:
        typer.echo("No profile found. Run `init` or `import` first.")
        raise typer.Exit(1)
    rows = active_facts([kind] if kind else None, include_pii=True)
    for fact in rows:
        flags = ("✓" if fact.verified else "·") + ("🔒" if fact.pii else "  ")
        typer.echo(f"{flags} [{fact.id}] ({fact.kind.value}) {fact.text}")
        if evidence and fact.evidence:
            typer.echo(f"      ↳ CV: {fact.evidence}")
    typer.echo(
        f"\n{len(rows)} facts (profile v{current[1]}). ✓ = verified (finalized), "
        "🔒 = PII, never sent to an LLM."
    )


@app.command()
def history(limit: int = 20) -> None:
    """Show recent profile versions."""
    rows = list_history(limit=limit)
    if not rows:
        typer.echo("No history yet.")
        return
    for r in rows:
        typer.echo(f"  v{r.version:<3} {r.created_at:%Y-%m-%d %H:%M}  {r.source:<8} {r.notes}")


@app.command()
def restore(version: int = typer.Argument(..., min=1)) -> None:
    """Restore a previous version (appends a new history row)."""
    try:
        restore_version(version)
    except ValueError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Restored v{version}. Run `validate` to confirm.")


if __name__ == "__main__":
    app()
from __future__ import annotations

import shutil
from pathlib import Path

import typer
from sqlalchemy import text

from .common import Table, console, err_console, settings_or_exit, typed_errors


@typed_errors
def cmd_init() -> None:
    """Create workspace folders, .env template, and the SQLite schema."""
    settings = settings_or_exit()
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    (settings.workspace_dir / "imported").mkdir(parents=True, exist_ok=True)
    (settings.workspace_dir / "exports").mkdir(parents=True, exist_ok=True)

    env = Path(".env")
    template = Path(__file__).resolve().parents[2].parent / ".env.example"
    if not env.exists() and template.exists():
        shutil.copy2(template, env)
        console.print(f"[green]created[/green] {env} (edit OLLAMA_MODEL)")
    elif not env.exists():
        console.print("[yellow]no .env and no template found; set environment variables manually[/yellow]")

    from ..db.engine import get_session
    from ..db.migrations import init_schema
    session = get_session(settings)
    try:
        applied = init_schema(session)
    finally:
        session.close()
    console.print(f"[green]database ready[/green] {settings.database_url}"
                  + (f" (applied {', '.join(applied)})" if applied else " (already current)"))
    console.print(f"[dim]absolute path: {settings.database_path}[/dim]")


@typed_errors
def cmd_health() -> None:
    """SQLite read/write test, Ollama reachability, configured model, parser capabilities.
    Sends no document text to any remote endpoint."""
    settings = settings_or_exit()
    table = Table(title="freight health")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")

    ok = True

    # SQLite read/write
    from ..db.engine import get_session
    from ..db.migrations import schema_status
    try:
        session = get_session(settings)
        try:
            session.execute(text("SELECT 1"))
            initialized, version = schema_status(session)
            table.add_row("sqlite", "[green]ok[/green]" if initialized else "[red]missing schema[/red]",
                          f"{settings.database_path} (schema v{version})" if initialized
                          else f"{settings.database_path} — run `freight init`")
            ok = ok and initialized
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        ok = False
        table.add_row("sqlite", "[red]fail[/red]", str(exc)[:120])

    # configuration sanity
    if not settings.ollama_model:
        ok = False
        table.add_row("model config", "[red]missing[/red]", "OLLAMA_MODEL is empty — set it in .env")
    else:
        table.add_row("model config", "[green]ok[/green]", settings.ollama_model)

    # Ollama reachable + model present (local endpoint only)
    from ..agents.model import check_ollama
    from ..config import is_local_endpoint
    if not is_local_endpoint(settings.ollama_base_url):
        ok = False
        table.add_row("ollama endpoint", "[red]rejected[/red]",
                      f"{settings.ollama_base_url} is not local; MVP forbids remote endpoints")
    else:
        reachable, detail, models = check_ollama(settings)
        table.add_row("ollama", "[green]ok[/green]" if reachable else "[red]unreachable[/red]",
                      detail if reachable else detail)
        if reachable and settings.ollama_model:
            table.add_row("ollama model", "[green]ok[/green]" if settings.ollama_model in models
                          else "[red]not installed[/red]",
                          f"available: {', '.join(models) or 'none'}")
            ok = ok and settings.ollama_model in models
        ok = ok and reachable

    # parser capabilities
    caps = []
    for name, mod in (("pdf", "pypdf"), ("docx", "docx"), ("xlsx", "openpyxl")):
        try:
            __import__(mod)
            caps.append(f"{name}:yes")
        except ImportError:
            caps.append(f"{name}:[red]no[/red]")
    table.add_row("parsers", "txt,eml always", ", ".join(caps))

    console.print(table)
    if not ok:
        err_console.print("[yellow]fix the issues above before running quotation or clarification processing.[/yellow]")
        raise typer.Exit(code=3)

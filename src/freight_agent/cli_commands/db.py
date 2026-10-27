from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from .common import console, settings_or_exit, typed_errors


@typed_errors
def backup(path: Path = typer.Argument(Path("./data/freight-backup.db"))) -> None:
    """Consistent SQLite backup via the backup API — never a raw file copy."""
    settings = settings_or_exit()
    from ..db.engine import backup_database
    dest = backup_database(settings, path)
    console.print(f"[green]backup written[/green] {dest}")


@typed_errors
def path() -> None:
    """Print the absolute path to the SQLite database file (open this in TablePlus)."""
    settings = settings_or_exit()
    db_path = settings.database_path
    console.print(str(db_path))
    if db_path.exists():
        console.print(f"[green]file exists[/green] ({db_path.stat().st_size} bytes)")
    else:
        console.print("[yellow]file does not exist yet — run `freight init` first[/yellow]")


@typed_errors
def open_db() -> None:
    """Open the database in TablePlus (macOS only)."""
    settings = settings_or_exit()
    db_path = settings.database_path
    if not db_path.exists():
        console.print("[red]database does not exist — run `freight init` first[/red]")
        raise typer.Exit(code=1)
    try:
        subprocess.run(["open", "-a", "TablePlus", str(db_path)], check=True)
        console.print(f"[green]opened[/green] {db_path} in TablePlus")
    except FileNotFoundError:
        console.print("[red]TablePlus not found — install it or open the database manually[/red]")
        console.print(f"Database path: {db_path}")
        raise typer.Exit(code=1)
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]failed to open TablePlus: {exc}[/red]")
        console.print(f"Database path: {db_path}")
        raise typer.Exit(code=1)

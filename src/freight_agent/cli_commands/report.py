from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ..services.comparison import export_report
from .common import console, err_console, settings_or_exit, typed_errors


@typed_errors
def export(
    shipment_ref: str,
    fmt: str = typer.Option("md", "--format", help="md | json | csv"),
    output: Optional[Path] = typer.Option(None, "--output",
                                          help="Target file; defaults to workspace/reports/<ref>-comparison-<ts>.<fmt>"),
) -> None:
    """Write a Workflow C comparison report to disk (md/json/csv)."""
    settings = settings_or_exit()
    path = export_report(settings, shipment_ref, fmt, output)
    console.print(f"[green]report exported[/green] {path}")
    err_console.print("[dim]deterministic output — no email sent, no ERP write-back[/dim]")

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ..services import intake as intake_service
from ..services.common import with_session
from .common import Table, console, err_console, settings_or_exit, typed_errors


@typed_errors
def add(
    shipment_id: str = typer.Argument(..., help="Quotation file, or shipment id when importing into an existing shipment."),
    file_path: Optional[Path] = typer.Argument(None, exists=True, dir_okay=False),
    source: str = typer.Option("upload", "--source", help="upload | email"),
    sender: Optional[str] = typer.Option(None, "--sender"),
    subject: Optional[str] = typer.Option(None, "--subject"),
    on_duplicate: str = typer.Option("error", "--on-duplicate", help="error | skip | reprocess"),
    auto: bool = typer.Option(True, "--auto/--no-auto",
                              help="Automatically classify, extract and resolve the shipment route after import."),
) -> None:
    """Import a quotation file: hash, store in workspace, extract text, then process the whole workflow."""
    settings = settings_or_exit()
    if file_path is None:
        if not Path(shipment_id).expanduser().is_file():
            # one argument that is not a file can only be a shipment id; the file is still missing
            err_console.print("Missing argument 'FILE_PATH'.\n"
                              "usage: freight intake add <file>  |  freight intake add <shipment-id> <file>")
            raise typer.Exit(2)
        file_path, shipment_id = Path(shipment_id).expanduser(), "none"
    sid = None if shipment_id.lower() in ("none", "-") else shipment_id
    source_type = "imported_email" if source == "email" else "upload"
    item, skipped = intake_service.register_intake(
        settings, sid, file_path, source=source_type, sender=sender, subject=subject,
        on_duplicate=on_duplicate,
    )
    if skipped:
        console.print(f"[yellow]duplicate content — skipped, existing intake id={item.id}[/yellow]")
        return
    console.print(f"[green]intake registered[/green]")
    console.print(f"  id: {item.id} [dim](later commands accept `last` instead of this id)[/dim]")
    console.print(f"  file: {item.original_filename}")
    console.print(f"  sha256: {item.content_sha256}")
    console.print(f"  chars: {len(item.raw_text)}")
    if not auto:
        console.print(f"[dim]View in TablePlus: freight db open[/dim]")
        return
    from ..domain.enums import ClassificationStatus
    from ..services.extraction import extract_and_store_quote
    console.print("[dim]auto-processing: classify → extract → resolve route…[/dim]")
    try:
        summary = extract_and_store_quote(settings, str(item.id))
        if any(n.startswith("classification is needs_review") for n in summary.notes):
            # local models are flaky: retry the classification once before asking a human
            console.print("[dim]low-confidence classification — retrying once…[/dim]")
            from ..services.classification import classify_intake
            item, _result = classify_intake(settings, str(item.id))
            if item.classification_status != ClassificationStatus.needs_review:
                summary = extract_and_store_quote(settings, str(item.id))
    except Exception as exc:  # noqa: BLE001 — intake stays registered; process manually after fixing the cause
        err_console.print(f"[yellow]auto-processing stopped: {exc}[/yellow]")
        err_console.print("[yellow]retry with: freight intake process last --force[/yellow]")
        return
    _print_summary(summary, dry_run=False)


@typed_errors
def classify_(intake_id: str, dry_run: bool = typer.Option(False, "--dry-run",
               help="Print the proposed classification without persisting it.")) -> None:
    from ..services.classification import classify_intake
    settings = settings_or_exit()
    item, result = classify_intake(settings, intake_id, dry_run=dry_run)
    console.print(f"classification: [bold]{item.classification_status}[/bold] "
                  f"(confidence {result.confidence:.2f})")
    console.print(f"reason: {result.reason}")
    if result.carrier_name:
        console.print(f"carrier: {result.carrier_name}")
    if result.shipment_reference:
        console.print(f"shipment reference: {result.shipment_reference}")
    if dry_run:
        err_console.print("[yellow]dry-run: nothing persisted[/yellow]")


@typed_errors
def process(
    intake_id: str,
    dry_run: bool = typer.Option(False, "--dry-run", help="Show proposed changes without persisting."),
    force: bool = typer.Option(False, "--force", help="Override an irrelevant/needs_review gate (audited)."),
    shipment: Optional[str] = typer.Option(None, "--shipment", help="Link an unassigned intake to this shipment ID or reference."),
) -> None:
    """Classify, extract, validate, resolve the shipment route automatically, save quotation/evidence/gaps."""
    from ..services.extraction import extract_and_store_quote
    settings = settings_or_exit()
    summary = extract_and_store_quote(
        settings, intake_id, dry_run=dry_run, force=force, shipment_reference=shipment,
    )
    _print_summary(summary, dry_run)


def _print_summary(summary, dry_run: bool) -> None:
    if summary.classification is not None:
        console.print(f"classified: {summary.classification.document_type} "
                      f"(quote={summary.classification.is_freight_quote}, "
                      f"conf={summary.classification.confidence:.2f})")
    if summary.shipment_id is not None:
        status = str(summary.shipment_status)
        color = "yellow" if status == "needs_review" else "green"
        console.print(f"shipment: [{color}]{summary.shipment_id} ({status})[/{color}]")
    if summary.quotation_id:
        console.print(f"[green]quotation created[/green] id={summary.quotation_id} "
                      f"status={summary.comparison_status}")
    console.print(f"gaps: {summary.gap_count} ({summary.critical_gap_count} critical)")
    for note in summary.notes:
        console.print(f"  • {note}")
    if dry_run:
        err_console.print("[yellow]dry-run: nothing persisted[/yellow]")


@typed_errors
def list_(
    shipment_id: Optional[str] = typer.Option(None, "--shipment-id"),
    status: Optional[str] = typer.Option(None, "--status"),
) -> None:
    settings = settings_or_exit()
    with with_session(settings) as repos:
        items = repos.list_intakes(shipment_id, status)
    table = Table(title="intake items")
    for col in ("id", "file", "source", "sha256", "classification", "confidence", "processing"):
        table.add_column(col)
    for i in items:
        table.add_row(str(i.id)[:8], i.original_filename, str(i.source_type),
                      i.content_sha256[:10], str(i.classification_status),
                      f"{i.classification_confidence:.2f}" if i.classification_confidence is not None else "-",
                      str(i.processing_status))
    console.print(table)


@typed_errors
def show(intake_id: str) -> None:
    settings = settings_or_exit()
    with with_session(settings) as repos:
        item = repos.get_intake(intake_id)
    console.print(f"[bold]{item.original_filename}[/bold] ({item.content_sha256[:16]}…)")
    console.print(f"source={item.source_type} status={item.classification_status}/{item.processing_status}")
    if item.classification_reason:
        console.print(f"reason: {item.classification_reason}")
    preview = item.raw_text[:600].replace("\n", " ")
    console.print(f"[dim]text preview: {preview}…[/dim]")


@typed_errors
def delete(intake_id: str) -> None:
    """Delete an intake item and all associated quotations, evidence, gaps, and drafts."""
    settings = settings_or_exit()
    with with_session(settings) as repos:
        item = repos.get_intake(intake_id)
        filename = item.original_filename
        repos.delete_intake_cascade(intake_id)
    console.print(f"[green]deleted[/green] intake {intake_id} ({filename}) and all associated data")

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from ..domain.exceptions import ValidationFailureError
from ..domain.models import SupplierReply
from ..services.common import with_session
from .common import console, settings_or_exit, typed_errors


class DuplicatePolicy(str, Enum):
    error = "error"
    skip = "skip"
    reprocess = "reprocess"


def _print(text: str) -> None:
    console.print(text, markup=False, highlight=False, soft_wrap=True)


def _show(item: SupplierReply) -> None:
    _print(f"Reply: {item.id} | shipment: {item.shipment_id}")
    _print(f"Carrier: {item.carrier_id or 'unknown'} | quotation: {item.quotation_id}")
    _print(f"Source intake: {item.intake_item_id} | association: {item.association_method} "
           f"| created at: {item.created_at}")


@typed_errors
def import_(
    shipment: str = typer.Argument(..., help="Shipment ID or reference."),
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Local supplier reply file."),
    carrier: Optional[str] = typer.Option(None, "--carrier", help="Carrier name or ID."),
    quote: Optional[str] = typer.Option(None, "--quote", help="Explicit quotation ID; required if association is uncertain."),
    on_duplicate: DuplicatePolicy = typer.Option(DuplicatePolicy.error, "--on-duplicate",
                                                 help="error, skip, or reprocess."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without persisting."),
) -> None:
    """Associate, extract, merge, recalculate, and create follow-up drafts automatically."""
    from ..domain.exceptions import ReplyAssociationRequiredError
    from ..services.replies import import_supplier_reply, preview_supplier_reply

    settings = settings_or_exit()
    with with_session(settings) as repos:
        shipment_record = repos.get_shipment(shipment)

    def import_or_preview(selected_quote):
        if dry_run:
            return preview_supplier_reply(settings, str(shipment_record.id), file, carrier_id=carrier,
                                          quote_id=selected_quote, on_duplicate=on_duplicate.value)
        item = import_supplier_reply(settings, str(shipment_record.id), file, carrier_id=carrier,
                                     quote_id=selected_quote, on_duplicate=on_duplicate.value)
        return item, None

    try:
        item, proposed = import_or_preview(quote)
    except ReplyAssociationRequiredError as exc:
        # This is the only service error handled before the outer typed_errors wrapper.
        # The service must not persist any intake/reply until association is resolved.
        _print("Reply association is uncertain. Candidate quotations:")
        with with_session(settings) as repos:
            for candidate in exc.candidates:
                candidate_carrier = (repos.get_carrier(str(candidate.carrier_id))
                                     if candidate.carrier_id else None)
                _print(f"Quotation: {candidate.id} | carrier: "
                       f"{candidate_carrier.name if candidate_carrier else 'unknown'} "
                       f"({candidate.carrier_id or 'unknown'}) | reference: "
                       f"{candidate.quote_reference or 'unknown'}")
        instruction = "Re-run freight reply import with --quote <quotation-id> from the candidates above."
        if not sys.stdin.isatty() or not exc.candidates:
            raise ValidationFailureError(f"Explicit reply association required. {instruction}") from exc
        selected = typer.prompt("Select quotation ID (copy the full candidate UUID)").strip()
        if selected not in {str(candidate.id) for candidate in exc.candidates}:
            raise ValidationFailureError(f"Selection is not a candidate quotation ID. {instruction}") from exc
        item, proposed = import_or_preview(selected)
    if dry_run:
        _print("Dry-run: proposed reply metadata (nothing persisted):")
        _print(item.model_dump_json(indent=2))
        _print("Proposed quotation:")
        _print(proposed.model_dump_json(indent=2))
    else:
        _show(item)
        _print("Reply import complete (automatic quotation processing; duplicate policy applied).")
        _print(f"Review follow-up drafts: freight email list {shipment_record.id}")


@typed_errors
def list_(shipment: str = typer.Argument(..., help="Shipment ID or reference.")) -> None:
    """List supplier reply associations without displaying original documents."""
    settings = settings_or_exit()
    with with_session(settings) as repos:
        shipment_record = repos.get_shipment(shipment)
        items = repos.list_replies(str(shipment_record.id))
        carriers = {str(item.carrier_id): repos.get_carrier(str(item.carrier_id))
                    for item in items if item.carrier_id}
    _print(f"Shipment: {shipment_record.id} ({shipment_record.reference})")
    if not items:
        _print("No supplier replies.")
    for item in items:
        carrier = carriers.get(str(item.carrier_id))
        _print(f"\nCarrier name: {carrier.name if carrier else 'unknown'}")
        _show(item)


@typed_errors
def show(reply_id: str) -> None:
    """Show reply association and provenance metadata, not the confidential source document."""
    settings = settings_or_exit()
    with with_session(settings) as repos:
        item = repos.get_reply(reply_id)
    _show(item)


@typed_errors
def reprocess(
    reply_id: str,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print proposed quotation JSON without persisting."),
) -> None:
    """Re-extract and merge a reply, recalculate its quotation, and generate follow-ups."""
    from ..services.replies import process_supplier_reply

    item = process_supplier_reply(settings_or_exit(), reply_id, dry_run=dry_run)
    if dry_run:
        _print("Dry-run: proposed quotation JSON (nothing persisted):")
        _print(item.model_dump_json(indent=2))
    else:
        _print(f"Reprocessed reply {reply_id} | quotation: {item.id} v{item.version} "
               f"| status: {item.comparison_status} | known total: {item.total_known_cost}")
        _print(f"Review follow-up drafts: freight email list {item.shipment_id}")


@typed_errors
def history(quote_id: str) -> None:
    """Show immutable quotation snapshots, versions, and source/model provenance metadata."""
    settings = settings_or_exit()
    with with_session(settings) as repos:
        quotation = repos.get_quotation(quote_id)
        versions = repos.versions_for_quotation(quote_id)
    _print(f"Quotation: {quotation.id} | current version: {quotation.version} "
           f"| carrier: {quotation.carrier_id or 'unknown'}")
    if not versions:
        _print("No quotation version snapshots.")
    for version in versions:
        _print(f"\nSnapshot: {version.id} | quotation: {version.quotation_id} v{version.version}")
        _print(f"Source intake: {version.source_intake_item_id} | created at: {version.created_at}")
        snapshot = json.loads(version.snapshot_json)
        _print("Snapshot JSON:\n" + json.dumps(snapshot, indent=2, ensure_ascii=False))
        # Show field provenance, never dump raw evidence snippets/documents automatically.
        evidence = json.loads(version.evidence_json)
        _print(f"Evidence records: {len(evidence)}")
        for field in evidence:
            metadata = {key: field[key] for key in (
                "id", "quotation_id", "field_name", "source_filename", "page_or_sheet",
                "confidence", "validation_status", "created_at",
            ) if key in field}
            _print(json.dumps(metadata, ensure_ascii=False))

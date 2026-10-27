from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

import typer
from click import edit as edit_text

from ..domain.models import ClarificationDraft
from ..services.common import with_session
from .common import console, settings_or_exit, typed_errors


class ExportFormat(str, Enum):
    eml = "eml"
    txt = "txt"


def _print(text: str) -> None:
    console.print(text, markup=False, highlight=False, soft_wrap=True)


def _summary(item: ClarificationDraft) -> None:
    _print(f"Draft: {item.id} | shipment: {item.shipment_id}")
    _print(f"Carrier: {item.carrier_id or 'unknown'} | quotation: {item.quotation_id} "
           f"v{item.quotation_version} | question set: {item.question_set_id}")
    _print(f"Status: {item.status} | approved at: {item.approved_at or 'not approved'}")


def _show(item: ClarificationDraft) -> None:
    _summary(item)
    _print(f"To: {item.recipient or '(not set; supply --to when editing or exporting)'}")
    _print(f"Subject: {item.subject}")
    _print("Body:\n" + item.body)


@typed_errors
def draft(
    question_set_id: str,
    carrier: Optional[str] = typer.Option(None, "--carrier", help="Carrier ID override."),
) -> None:
    """Create an email draft from a question set (questions generate already creates drafts)."""
    from ..services.clarification import create_email_draft

    item = create_email_draft(settings_or_exit(), question_set_id, carrier_id=carrier)
    _summary(item)
    _print(f"Review full content: freight email show {item.id}")


@typed_errors
def list_(shipment: str = typer.Argument(..., help="Shipment ID or reference.")) -> None:
    """List carrier-separated drafts and their approval status, without printing bodies."""
    settings = settings_or_exit()
    with with_session(settings) as repos:
        shipment_record = repos.get_shipment(shipment)
        drafts = repos.list_drafts(str(shipment_record.id))
        carriers = {str(item.carrier_id): repos.get_carrier(str(item.carrier_id))
                    for item in drafts if item.carrier_id}
    _print(f"Shipment: {shipment_record.id} ({shipment_record.reference})")
    if not drafts:
        _print("No email drafts.")
    for item in drafts:
        carrier = carriers.get(str(item.carrier_id))
        _print(f"\nCarrier name: {carrier.name if carrier else 'unknown'}")
        _summary(item)


@typed_errors
def show(draft_id: str) -> None:
    """Show the whole recipient, subject, and body for review."""
    settings = settings_or_exit()
    with with_session(settings) as repos:
        item = repos.get_draft(draft_id)
    _show(item)


@typed_errors
def edit(
    draft_id: str,
    subject: Optional[str] = typer.Option(None, "--subject"),
    body_file: Optional[Path] = typer.Option(None, "--body-file", exists=True, dir_okay=False,
                                           help="UTF-8 text file containing the replacement body."),
    to: Optional[str] = typer.Option(None, "--to", help="Recipient email address."),
) -> None:
    """Edit a draft; with no flags, open its body in your local editor. Edits require reapproval."""
    from ..services.clarification import edit_draft

    settings = settings_or_exit()
    body = body_file.read_text(encoding="utf-8") if body_file is not None else None
    if subject is None and body_file is None and to is None:
        with with_session(settings) as repos:
            item = repos.get_draft(draft_id)
        body = edit_text(item.body)
        if body is None:
            _print("Editor cancelled or body unchanged; draft not modified.")
            return
    edit_draft(settings, draft_id, subject=subject, body=body, recipient=to)
    _print(f"Updated draft {draft_id}. Review its full content and approve again before export.")


@typed_errors
def approve(draft_id: str) -> None:
    """Review the entire draft and explicitly confirm approval. No --yes bypass; never sends email."""
    from ..services.clarification import approve_draft

    settings = settings_or_exit()
    with with_session(settings) as repos:
        item = repos.get_draft(draft_id)
    _show(item)
    if not typer.confirm("Approve this exact draft for local export? (No email will be sent)",
                         default=False):
        _print("Approval declined; draft unchanged.")
        raise typer.Exit(code=1)
    approve_draft(settings, draft_id)
    _print(f"Approved draft {draft_id} for local export only. No email sent.")


@typed_errors
def export(
    draft_id: str,
    format: ExportFormat = typer.Option(..., "--format", help="Local export format: eml or txt."),
    output: Optional[Path] = typer.Option(None, "--output", dir_okay=False, help="Output file path."),
    to: Optional[str] = typer.Option(None, "--to", help="Explicit recipient for this export."),
) -> None:
    """Export an approved draft to a local file. No send function or network delivery."""
    from ..services.clarification import export_draft

    path = export_draft(settings_or_exit(), draft_id, format=format.value,
                        output_path=output, recipient=to)
    _print(f"Exported: {Path(path).resolve()} (local file only; no email sent).")

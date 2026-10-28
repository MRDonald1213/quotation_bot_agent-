from __future__ import annotations

from typing import Optional

import typer

from ..services.common import with_session
from .common import console, settings_or_exit, typed_errors


def _print(text: str) -> None:
    console.print(text, markup=False, highlight=False, soft_wrap=True)


@typed_errors
def generate(
    shipment: str = typer.Argument(..., help="Shipment ID or reference."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without persisting questions or drafts."),
) -> None:
    """Generate questions AND email drafts for every quotation in a shipment."""
    from ..services.clarification import generate_clarification_drafts

    settings = settings_or_exit()
    with with_session(settings) as repos:
        shipment_record = repos.get_shipment(shipment)
    drafts = generate_clarification_drafts(settings, str(shipment_record.id), dry_run=dry_run)
    _print(f"{'Proposed' if dry_run else 'Generated'} {len(drafts)} clarification draft(s) "
           f"for shipment {shipment_record.id} ({shipment_record.reference}).")
    # Automatic output is metadata only, not supplier document contents or draft bodies.
    for draft in drafts:
        _print(f"Carrier: {draft.carrier_id or 'unknown'} | quotation: {draft.quotation_id} "
               f"v{draft.quotation_version}")
        _print(f"Question set: {draft.question_set_id} | draft: {draft.id} | status: {draft.status}")
        if dry_run:
            _print(draft.model_dump_json(indent=2))
    if dry_run:
        _print("Dry-run: nothing persisted; proposed IDs are not saved records.")
    elif drafts:
        _print(f"Review questions: freight questions list {shipment_record.id}")
        _print("Approve and export each draft in `freight review` (one button) or with "
               "freight email show/approve/export <draft-id>.")


@typed_errors
def list_(shipment: str = typer.Argument(..., help="Shipment ID or reference.")) -> None:
    """Show carrier-separated questions, reasons, source context, and draft approval status."""
    settings = settings_or_exit()
    with with_session(settings) as repos:
        shipment_record = repos.get_shipment(shipment)
        sets = repos.list_question_sets(str(shipment_record.id))
        drafts = repos.list_drafts(str(shipment_record.id))
        carriers = {str(item.carrier_id): repos.get_carrier(str(item.carrier_id))
                    for item in sets if item.carrier_id}
    _print(f"Shipment: {shipment_record.id} ({shipment_record.reference})")
    if not sets:
        _print("No clarification question sets.")
    for item in sets:
        carrier = carriers.get(str(item.carrier_id))
        _print(f"\nCarrier: {carrier.name if carrier else 'unknown'} ({item.carrier_id or 'unknown'})")
        _print(f"Question set: {item.id} | quotation: {item.quotation_id} v{item.quotation_version}")
        _print(f"Status: {item.status} | approved at: {item.approved_at or 'not approved'}")
        _print(f"Model: {item.model_name or 'unknown'} | prompt: {item.prompt_version or 'unknown'}")
        for draft in drafts:
            if draft.question_set_id == item.id:
                _print(f"Draft: {draft.id} | status: {draft.status} "
                       f"| approved at: {draft.approved_at or 'not approved'}")
        for index, question in enumerate(item.questions, start=1):
            _print(f"{index}. [{question.priority}] {question.field_name}: {question.question_text}")
            _print(f"   Why: {question.why_it_matters}")
            _print(f"   Source context: {question.source_context}")


@typed_errors
def edit(
    question_set_id: str,
    index: int = typer.Option(..., "--index", min=1, help="Question number (1-based)."),
    text: Optional[str] = typer.Option(None, "--text", help="Replacement question; prompts if omitted."),
) -> None:
    """Edit a question; its draft must be reviewed and approved again."""
    from ..services.clarification import edit_question

    settings = settings_or_exit()
    replacement = text if text is not None else typer.prompt("Replacement question text")
    if not replacement.strip():
        raise typer.BadParameter("Question text must not be empty.", param_hint="--text")
    edit_question(settings, question_set_id, index=index, text=replacement)
    _print(f"Updated question {index} in question set {question_set_id}. Review its draft before approval.")

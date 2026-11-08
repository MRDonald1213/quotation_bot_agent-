from __future__ import annotations

from typing import Optional

import typer

from ..domain.money import format_money
from ..services.common import with_session
from .common import Table, console, settings_or_exit, typed_errors


@typed_errors
def list_(shipment_id: Optional[str] = typer.Option(None, "--shipment-id"),
          archived: bool = typer.Option(False, "--archived")) -> None:
    from decimal import Decimal
    from ..services.workspace import list_quotes
    rows = list_quotes(settings_or_exit(), archived)
    if shipment_id:
        rows = [row for row in rows if row["shipment_id"] == shipment_id]
    table = Table(title="Archived quotations" if archived else "Quotations")
    for column in ("id", "supplier", "lane", "reference", "price", "unit", "stage"):
        table.add_column(column)
    for row in rows:
        table.add_row(row["id"][:8], row["supplier"] or "Not stated",
            f"{row['origin'] or 'Unknown'} → {row['destination'] or 'Unknown'}",
            row["quote_reference"] or "Not stated",
            format_money(Decimal(row["base_price"]) if row["base_price"] is not None else None, row["currency"]),
            row["details"]["pricing_unit"] or "Unknown", row["stage"])
    console.print(table)


@typed_errors
def reprocess(quote_id: str) -> None:
    """Re-extract the original intake; preserve quotation/evidence snapshots and gap history."""
    from ..services.extraction import extract_and_store_quote
    settings = settings_or_exit()
    with with_session(settings) as repos:
        quote = repos.get_quotation(quote_id)
    summary = extract_and_store_quote(settings, str(quote.intake_item_id), force=True)
    console.print(f"[green]reprocessed[/green] quotation {summary.quotation_id} "
                  f"status={summary.comparison_status} gaps={summary.gap_count}")


@typed_errors
def show(quote_id: str) -> None:
    """Show a quote with confidence, evidence, and gaps (workflow A step 9)."""
    settings = settings_or_exit()
    with with_session(settings) as repos:
        quote = repos.get_quotation(quote_id)
        evidence = repos.evidence_for_quotation(quote_id)
        gaps = [g for g in repos.list_gaps(str(quote.shipment_id))
                if str(g.quotation_id) == str(quote.id)]
        carrier = repos.get_carrier(str(quote.carrier_id)) if quote.carrier_id else None

    console.print(f"[bold]quotation {quote.id}[/bold] v{quote.version} — {str(quote.comparison_status)}")
    console.print(f"carrier: {carrier.name if carrier else 'unknown'} | reference: "
                  f"{quote.quote_reference or 'unknown'} | model: {quote.model_name or '?'} "
                  f"(prompt {quote.prompt_version or '?'})")
    console.print(f"origin/destination: {quote.origin or '?'} → {quote.destination or '?'} | "
                  f"scope: {quote.delivery_scope or 'unknown'}")
    console.print(f"base price: {format_money(quote.base_price, quote.currency)} | "
                  f"known total: {format_money(quote.total_known_cost, quote.currency)}"
                  " (deterministic subtotal — unknown charges are not zero)")
    console.print(f"transit: {quote.transit_days_min or '?'}–{quote.transit_days_max or '?'} days | "
                  f"valid until: {quote.valid_until or 'unknown'} | "
                  f"insurance: {quote.insurance_included} | customs: {quote.customs_included}")
    if quote.known_additional_charges:
        ct = Table(title="charges")
        ct.add_column("label")
        ct.add_column("amount")
        ct.add_column("in base?")
        ct.add_column("mandatory?")
        for ch in quote.known_additional_charges:
            ct.add_row(ch.label, format_money(ch.amount, ch.currency),
                       str(ch.included_in_base_price), str(ch.mandatory))
        console.print(ct)
    if quote.assumptions:
        console.print("[italic]assumptions: " + "; ".join(quote.assumptions) + "[/italic]")

    et = Table(title="field evidence")
    et.add_column("field")
    et.add_column("value")
    et.add_column("source")
    et.add_column("snippet")
    et.add_column("conf")
    et.add_column("validation")
    for e in evidence:
        et.add_row(e.field_name, e.value_as_extracted[:20], e.source_filename[:24],
                   (e.snippet[:60] + "…") if len(e.snippet) > 60 else e.snippet,
                   f"{e.confidence:.2f}" if e.confidence is not None else "-",
                   str(e.validation_status))
    console.print(et)

    if gaps:
        gt = Table(title="gaps")
        gt.add_column("field")
        gt.add_column("type")
        gt.add_column("severity")
        gt.add_column("status")
        gt.add_column("description")
        for g in gaps:
            gt.add_row(g.field_name, str(g.gap_type), str(g.severity), str(g.status), g.description)
        console.print(gt)
    else:
        console.print("[green]no open gaps[/green]")


@typed_errors
def archive(quote_id: str, reason: str = typer.Option(..., "--reason"), disposition: str = typer.Option("archived", "--disposition")) -> None:
    from ..services.workspace import archive as archive_quote
    archive_quote(settings_or_exit(), quote_id, disposition, reason)
    console.print("Quotation archived; history preserved")


@typed_errors
def approve(quote_id: str) -> None:
    from ..services.workspace import approve as approve_quote
    approve_quote(settings_or_exit(), quote_id)
    console.print("Quotation approved; import remains separate")


@typed_errors
def import_(quote_id: str) -> None:
    from ..services.workspace import import_quote
    console.print(import_quote(settings_or_exit(), quote_id))


@typed_errors
def ask(quote_id: str) -> None:
    from ..services.workspace import start_questions
    console.print(start_questions(settings_or_exit(), quote_id))


@typed_errors
def answer(quote_id: str, turn_id: str, text: str) -> None:
    from ..services.workspace import answer as answer_questions
    console.print(answer_questions(settings_or_exit(), quote_id, turn_id, text))


@typed_errors
def resolve(quote_id: str, evidence_id: str, reason: str = typer.Option(..., "--reason"), use_candidate: bool = typer.Option(False, "--use-candidate")) -> None:
    from ..services.workspace import resolve as resolve_conflict
    console.print(resolve_conflict(settings_or_exit(), quote_id, evidence_id, use_candidate, reason))

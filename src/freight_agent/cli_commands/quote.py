from __future__ import annotations

from typing import Optional

import typer

from ..domain.money import format_money
from ..services.common import with_session
from .common import Table, console, settings_or_exit, typed_errors


@typed_errors
def list_(shipment_id: Optional[str] = typer.Option(None, "--shipment-id")) -> None:
    settings = settings_or_exit()
    with with_session(settings) as repos:
        quotes = repos.list_quotations(shipment_id)
        carriers = {str(c.id): c.name for c in
                    (repos.get_carrier(str(q.carrier_id)) for q in quotes if q.carrier_id) if c}
    table = Table(title="quotations")
    for col in ("id", "carrier", "ref", "base price", "known total", "valid until", "status"):
        table.add_column(col)
    for q in quotes:
        table.add_row(
            str(q.id)[:8],
            carriers.get(str(q.carrier_id), "?") if q.carrier_id else "?",
            q.quote_reference or "-",
            format_money(q.base_price, q.currency),
            format_money(q.total_known_cost, q.currency),
            str(q.valid_until or "unknown"),
            str(q.comparison_status),
        )
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

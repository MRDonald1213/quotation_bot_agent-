from __future__ import annotations

import typer

from ..services.comparison import waive_gap
from ..services.gap_analysis import shipment_gap_report
from .common import Table, console, settings_or_exit, typed_errors


@typed_errors
def gaps(shipment_ref: str) -> None:
    """Display detected gaps per quote for a shipment (workflow A step 7+9)."""
    settings = settings_or_exit()
    shipment, results = shipment_gap_report(settings, shipment_ref)
    console.print(f"[bold]shipment {shipment.reference}[/bold] "
                  f"({shipment.origin} → {shipment.destination}, "
                  f"required scope {shipment.required_delivery_scope}, {shipment.target_currency})")
    if not results:
        console.print("[yellow]no quotations recorded for this shipment yet[/yellow]")
        return
    for quote, analysis in results:
        console.print(f"\n[bold]quote {str(quote.id)[:8]}[/bold] — status {quote.comparison_status} "
                      f"— readiness {analysis.comparison_readiness}")
        console.print(analysis.summary)
        if not analysis.gaps:
            console.print("  [green]no gaps[/green]")
            continue
        table = Table()
        table.add_column("field")
        table.add_column("type")
        table.add_column("severity")
        table.add_column("description")
        table.add_column("suggested question")
        for g, q in zip(analysis.gaps, analysis.clarification_questions + [None] * len(analysis.gaps)):
            table.add_row(g.field_name, str(g.gap_type), str(g.severity),
                          g.description[:80], q.question_text if q else "-")
        console.print(table)


@typed_errors
def waive(
    gap_id: str,
    reason: str = typer.Option(..., "--reason", help="Why the gap is accepted; recorded in the audit log."),
) -> None:
    """Waive an open data gap with a written reason (human decision, audited)."""
    settings = settings_or_exit()
    gap = waive_gap(settings, gap_id, reason)
    console.print(f"[green]gap waived[/green] {gap.id} ({gap.field_name}, {gap.severity})")

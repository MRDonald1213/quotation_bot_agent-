from __future__ import annotations

import typer

from ..domain.models import ComparisonReport
from ..services.comparison import build_comparison, render_report
from .common import Table, console, err_console, settings_or_exit, typed_errors


def _print_table(report: ComparisonReport) -> None:
    console.print(f"[bold]comparison — shipment {report.shipment_reference}[/bold] "
                  f"{report.route}  (required scope {report.required_scope}, "
                  f"target {report.target_currency})")
    if not report.rows:
        console.print("[yellow]no quotations recorded for this shipment[/yellow]")
        return
    table = Table()
    for col in ("carrier", "status", "currency", "base", "extras", "known subtotal",
                report.target_currency.lower(), "scope", "transit", "valid until", "crit gaps"):
        table.add_column(col)
    for r in report.rows:
        table.add_row(
            r.carrier_name or "-", str(r.comparison_status), r.currency or "-",
            str(r.base_price) if r.base_price is not None else "-",
            str(r.known_additional_total) if r.known_additional_total is not None else "-",
            str(r.known_subtotal) if r.known_subtotal is not None else "-",
            str(r.converted_subtotal) if r.converted_subtotal is not None else "no FX",
            r.delivery_scope, r.transit_time,
            str(r.valid_until) if r.valid_until else "-",
            str(len(r.unresolved_critical_gaps)),
        )
    console.print(table)
    for r in report.rows:
        for gap in r.unresolved_critical_gaps:
            err_console.print(f"  critical [{r.carrier_name or r.quote_reference or str(r.quotation_id)[:8]}]: {gap}")
        for unc in r.uncertain_charges:
            err_console.print(f"  uncertain [{r.carrier_name or r.quote_reference or str(r.quotation_id)[:8]}]: {unc}")
    if report.recommendation:
        console.print(f"\n[green]recommendation:[/green] {report.recommendation}")
        for basis in report.recommendation_basis:
            console.print(f"  [dim]{basis}[/dim]")
    else:
        console.print("\n[yellow]recommendation: none — not enough comparable data[/yellow]")
    for caveat in report.caveats:
        console.print(f"  • {caveat}")


@typed_errors
def comparison(
    shipment_ref: str,
    fmt: str = typer.Option("table", "--format", help="table | json | csv | md"),
) -> None:
    """Deterministic cross-carrier comparison for one shipment (Workflow C)."""
    settings = settings_or_exit()
    report = build_comparison(settings, shipment_ref)
    if fmt == "table":
        _print_table(report)
    else:
        print(render_report(report, fmt))

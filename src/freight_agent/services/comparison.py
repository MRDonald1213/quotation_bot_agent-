from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..config import Settings
from ..domain.enums import ComparisonStatus, GapSeverity, GapStatus
from ..domain.exceptions import ValidationFailureError
from ..domain.models import (
    AuditEvent,
    ComparisonReport,
    ComparisonRow,
    Gap,
    Quotation,
    Shipment,
)
from ..db.repositories import Repos
from .common import with_session
from .gap_analysis import detect_gaps_deterministic
from . import money


def _convert(subtotal: Decimal | None, currency: str | None, target: str,
             settings: Settings) -> tuple[Decimal | None, str | None]:
    """Return (amount-in-target, note). Never invents a rate: an unresolvable pair
    leaves the quote unrankable rather than silently summed."""
    if subtotal is None or currency is None:
        return None, None
    if currency.upper() != target.upper() and f"{currency.upper()}->{target.upper()}" not in money.configured_rates(settings):
        return None, "No explicit configured FX rate"
    return money.convert(subtotal, currency, target, settings)


def _known_breakdown(quote: Quotation) -> tuple[Decimal | None, list[str]]:
    """Known subtotal = base + only the charges that are explicit numeric, confirmed
    additional, and in the quote currency. Uncertain charges are listed, never zeroed."""
    uncertain: list[str] = []
    if quote.base_price is None or quote.currency is None:
        for ch in quote.known_additional_charges:
            uncertain.append(f"{ch.label} (no base/currency to anchor)")
        return None, uncertain
    subtotal = quote.base_price
    for ch in quote.known_additional_charges:
        if ch.included_in_base_price is True:
            continue
        if ch.amount is None or ch.included_in_base_price is None:
            if ch.mandatory is not False:
                uncertain.append(f"{ch.label} (amount or inclusion unclear)")
            continue
        if ch.currency is None or ch.currency != quote.currency:
            uncertain.append(f"{ch.label} (currency {ch.currency or '?'} ≠ {quote.currency})")
            continue
        if (ch.pricing_unit and quote.details.pricing_unit and ch.pricing_unit != quote.details.pricing_unit) or (quote.details.pricing_unit not in {None, "shipment"} and not ch.pricing_unit):
            uncertain.append(f"{ch.label} (pricing basis incompatible or unknown)")
            continue
        subtotal += ch.amount
    return subtotal, uncertain


def _transit(quote: Quotation) -> str:
    lo, hi = quote.transit_days_min, quote.transit_days_max
    if lo is not None and hi is not None:
        return f"{lo}–{hi} days" if lo != hi else f"{lo} days"
    if lo is not None:
        return f"{lo}+ days"
    if hi is not None:
        return f"up to {hi} days"
    return "-"


def _row_for(repos: Repos, shipment: Shipment, quote: Quotation,
             settings: Settings, carrier_name: str | None) -> ComparisonRow:
    subtotal, uncertain = _known_breakdown(quote)
    converted, fx_note = _convert(subtotal, quote.currency, shipment.target_currency, settings)
    usd_subtotal, usd_note = _convert(subtotal, quote.currency, "USD", settings)

    quote_gaps = [g for g in repos.list_gaps(str(shipment.id)) if g.quotation_id == quote.id]
    if not quote_gaps:
        # quote never analysed (created outside the process command): derive gaps now
        quote_gaps = [
            Gap(shipment_id=shipment.id, quotation_id=quote.id, field_name=g.field_name,
                gap_type=g.gap_type, severity=g.severity, description=g.description)
            for g in detect_gaps_deterministic(shipment, quote)
        ]
    open_gaps = [g for g in quote_gaps if g.status == GapStatus.open]
    critical = [g for g in open_gaps if g.severity == GapSeverity.critical]
    other = [g for g in open_gaps if g.severity != GapSeverity.critical]

    scope_compatible = (str(shipment.required_delivery_scope) == "unknown"
                        or quote.delivery_scope is None
                        or quote.delivery_scope == shipment.required_delivery_scope)

    if quote.valid_until is not None and quote.valid_until < date.today():
        status = ComparisonStatus.expired
    elif any(g.field_name == "delivery_scope" and str(g.gap_type) == "scope_mismatch" for g in critical):
        status = ComparisonStatus.needs_review
    elif critical:
        status = ComparisonStatus.incomplete
    else:
        status = ComparisonStatus.comparable

    evidence_refs = [
        f"{e.field_name}: {e.source_filename}"
        + (f"[{e.page_or_sheet}]" if e.page_or_sheet else "")
        + f" “{e.snippet[:60]}”"
        for e in repos.evidence_for_quotation(str(quote.id))
    ]

    from .workspace import readiness
    missing, conflicts, blocking_gaps = readiness(repos, quote)
    exclusions = []
    if quote.disposition or quote.review_status != "imported":
        exclusions.append("Quotation is not active/imported")
    if missing or conflicts or blocking_gaps or status != ComparisonStatus.comparable:
        exclusions.append("Missing, expired or unresolved information")
    if not scope_compatible or uncertain:
        exclusions.append("Scope or charges are not comparable")
    if not quote.details.pricing_unit:
        exclusions.append("Pricing unit is unknown")
    if converted is None:
        exclusions.append(fx_note or "No comparable total or explicit FX rate")
    if ((quote.origin or "").strip().casefold() != shipment.origin.strip().casefold()
            or (quote.destination or "").strip().casefold() != shipment.destination.strip().casefold()):
        exclusions.append("Quotation route differs from selected shipment")

    return ComparisonRow(
        pricing_unit=quote.details.pricing_unit, transit_days=quote.transit_days_max, exclusions=exclusions,
        quotation_id=quote.id, carrier_name=carrier_name, quote_reference=quote.quote_reference,
        currency=quote.currency, base_price=quote.base_price,
        known_additional_total=(None if subtotal is None or quote.base_price is None
                                else subtotal - quote.base_price),
        known_subtotal=subtotal, converted_subtotal=converted, fx_note=fx_note,
        usd_subtotal=usd_subtotal, usd_note=usd_note,
        uncertain_charges=uncertain, included_services=quote.included_services,
        excluded_services=quote.excluded_services, delivery_scope=str(quote.delivery_scope or "unknown"),
        transit_time=_transit(quote), valid_until=quote.valid_until, comparison_status=status,
        scope_compatible=scope_compatible,
        unresolved_critical_gaps=[g.description for g in critical],
        unresolved_other_gaps=[g.description for g in other],
        evidence_refs=evidence_refs,
    )


def build_comparison(settings: Settings, id_or_reference: str, pricing_unit: str | None = None) -> ComparisonReport:
    """Workflow C: deterministic cross-carrier comparison. No LLM, no invented totals."""
    with with_session(settings) as repos:
        shipment = repos.get_shipment(id_or_reference)
        quotes = repos.list_quotations(str(shipment.id))
        rows: list[ComparisonRow] = []
        for q in quotes:
            carrier = repos.get_carrier(str(q.carrier_id)) if q.carrier_id else None
            rows.append(_row_for(repos, shipment, q, settings, carrier.name if carrier else None))
        eligible_units = {r.pricing_unit for r in rows if not r.exclusions}
        for row in rows:
            if pricing_unit and row.pricing_unit != pricing_unit:
                row.exclusions.append("Different pricing unit")
            elif not pricing_unit and len(eligible_units) > 1 and not row.exclusions:
                row.exclusions.append("Select one pricing unit before comparing")
        return _assemble(shipment, rows)


def _assemble(shipment: Shipment, rows: list[ComparisonRow]) -> ComparisonReport:
    report = ComparisonReport(
        shipment_id=shipment.id, shipment_reference=shipment.reference,
        route=f"{shipment.origin} → {shipment.destination}",
        required_scope=str(shipment.required_delivery_scope),
        target_currency=shipment.target_currency, rows=rows,
    )
    if not rows:
        report.caveats.append("No quotations recorded for this shipment yet.")
        return report

    eligible = [
        r for r in rows
        if not r.exclusions and r.comparison_status == ComparisonStatus.comparable
        and r.scope_compatible and r.converted_subtotal is not None
    ]
    for r in rows:
        report.caveats.extend(f"{r.carrier_name or r.quote_reference}: {reason}" for reason in r.exclusions)
        if r.comparison_status != ComparisonStatus.comparable:
            report.caveats.append(f"{r.carrier_name or r.quote_reference or str(r.quotation_id)[:8]}: "
                                  f"{r.comparison_status.value}")
        if not r.scope_compatible:
            report.caveats.append(f"{r.carrier_name or r.quote_reference}: delivery scope "
                                  f"{r.delivery_scope} differs from required {shipment.required_delivery_scope}; "
                                  "excluded from ranking")
        if r.fx_note:
            report.caveats.append(f"{r.carrier_name or r.quote_reference}: {r.fx_note}")

    if len(eligible) >= 1:
        ranked = sorted(eligible, key=lambda r: r.converted_subtotal)  # type: ignore[arg-type]
        best = ranked[0]
        carrier = best.carrier_name or best.quote_reference or str(best.quotation_id)[:8]
        usd_part = (f" — USD {best.usd_subtotal}" if best.usd_subtotal is not None
                    and str(shipment.target_currency).upper() != "USD" else "")
        report.recommendation = (
            f"Lowest known total in {shipment.target_currency}: {carrier} at "
            f"{best.converted_subtotal}{usd_part} (base {best.base_price} {best.currency})."
        )
        report.recommendation_basis = [
            "Ranked by known subtotal (base + confirmed additional charges), not base price alone.",
            f"Only quotes that are comparable, scope-compatible, and convertible to {shipment.target_currency} were ranked.",
        ]
        if any(r.usd_note for r in rows):
            report.recommendation_basis.append(
                f"USD figures use the local reference rate table (as of {money.RATES_AS_OF}); "
                "set FX_RATES_JSON in .env to override a pair.")
        if best.fx_note:
            report.recommendation_basis.append(f"Winner uses configured FX: {best.fx_note}")
        if best.unresolved_other_gaps:
            report.caveats.append(
                f"{carrier} still has non-critical unknowns: " + "; ".join(best.unresolved_other_gaps))
        report.caveats.append("Recommendation is advisory; a human makes the final carrier decision.")
    else:
        report.recommendation = None
        report.caveats.append("No quote is fully comparable yet — resolve critical gaps or configure FX to rank.")
    return report


def waive_gap(settings: Settings, gap_id: str, reason: str) -> Gap:
    """Human decision to proceed despite an open gap. Audited (spec §10)."""
    if not reason.strip():
        raise ValidationFailureError("a written reason is required to waive a gap")
    with with_session(settings) as repos, repos.atomic():
        gap = repos.get_gap(gap_id)
        if gap.status != GapStatus.open:
            raise ValidationFailureError(f"gap {str(gap.id)[:8]} is {gap.status}, not open; nothing to waive")
        gap.status = GapStatus.waived
        gap.resolved_at = datetime.now(timezone.utc).replace(microsecond=0)
        repos.update_gap(gap)
        repos.add_audit(AuditEvent(
            entity_type="gap", entity_id=gap.id, action="waived",
            details_json=json.dumps({"reason": reason.strip(),
                                     "field": gap.field_name, "severity": str(gap.severity)}),
        ))
        return gap


REPORT_FORMATS = ("table", "json", "csv", "md")


def render_report(report: ComparisonReport, fmt: str) -> str:
    if fmt == "json":
        return report.model_dump_json(indent=2)
    if fmt not in ("csv", "md", "table"):
        raise ValidationFailureError(f"unknown format {fmt!r}; choose from {', '.join(REPORT_FORMATS)}")
    headers = ["carrier", "status", "currency", "base_price", "known_extras", "known_subtotal",
               f"in_{report.target_currency}", "in_USD", "scope", "transit", "valid_until",
               "critical_gaps", "uncertain_charges"]
    body = []
    for r in report.rows:
        body.append([
            r.carrier_name or "-", str(r.comparison_status), r.currency or "-",
            "" if r.base_price is None else str(r.base_price),
            "" if r.known_additional_total is None else str(r.known_additional_total),
            "" if r.known_subtotal is None else str(r.known_subtotal),
            "" if r.converted_subtotal is None else str(r.converted_subtotal),
            "" if r.usd_subtotal is None else str(r.usd_subtotal),
            r.delivery_scope, r.transit_time,
            "" if r.valid_until is None else r.valid_until.isoformat(),
            "; ".join(r.unresolved_critical_gaps), "; ".join(r.uncertain_charges),
        ])
    if fmt == "csv":
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([f"shipment {report.shipment_reference}", report.route,
                         f"target {report.target_currency}", f"generated {report.generated_at.isoformat()}"])
        writer.writerow(headers)
        writer.writerows(body)
        if report.recommendation:
            writer.writerow([])
            writer.writerow(["recommendation", report.recommendation])
        for caveat in report.caveats:
            writer.writerow(["caveat", caveat])
        return buf.getvalue()
    lines = [
        f"# Freight comparison — {report.shipment_reference}",
        "",
        f"Route: {report.route}  |  required scope: {report.required_scope}  |  "
        f"target currency: {report.target_currency}  |  generated: {report.generated_at.isoformat()}",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
    ]
    for row in body:
        lines.append("| " + " | ".join(str(c).replace("|", "/") for c in row) + " |")
    lines.append("")
    if report.recommendation:
        lines += ["## Recommendation", report.recommendation, ""]
        lines += [f"- {b}" for b in report.recommendation_basis]
    else:
        lines += ["## Recommendation", "None — not enough comparable data to rank responsibly."]
    if report.caveats:
        lines += ["", "## Caveats"] + [f"- {c}" for c in report.caveats]
    return "\n".join(lines) + "\n"


def export_report(settings: Settings, id_or_reference: str, fmt: str,
                  output_path: Path | None = None) -> Path:
    if fmt == "table":
        raise ValidationFailureError("the table view is for terminal display; export as md, json or csv")
    report = build_comparison(settings, id_or_reference)
    if output_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output_path = (settings.workspace_dir / "reports"
                       / f"{report.shipment_reference}-comparison-{stamp}.{fmt}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(report, fmt), encoding="utf-8")
    return output_path

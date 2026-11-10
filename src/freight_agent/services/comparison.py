from __future__ import annotations

import json
import csv
import re
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


def compare_supplier_master_quotes(settings: Settings) -> dict:
    """Compare quote rows from the single supplier master only on identical terms."""
    from .supplier_databases import SUPPLIER_MASTER_FILE

    if not SUPPLIER_MASTER_FILE.is_file():
        return {"quotes": [], "groups": [], "data_note": "Supplier_Master_Database.csv is missing."}
    with SUPPLIER_MASTER_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    quotes = []
    by_comparison_key: dict[tuple[str, ...], list[dict]] = {}
    required_for_comparison = ("Origin_City", "Origin_Country", "Destination_City", "Destination_Country",
                               "Transport_Mode", "Cargo_Type", "Currency", "Pricing_Basis", "Fuel_Type",
                               "Quotation_Valid_Until")
    demo_statuses = {"high_confidence", "demo"}
    for raw in rows:
        if not str(raw.get("Quotation_ID") or "").strip():
            continue
        try:
            confidence = json.loads(raw.get("Extraction_Confidence_JSON") or "{}")
            if not isinstance(confidence, dict):
                confidence = {}
        except (TypeError, ValueError):
            confidence = {}
        try:
            evidence = json.loads(raw.get("Extraction_Evidence_JSON") or "{}")
            if isinstance(evidence, list):
                evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
        except (TypeError, ValueError):
            evidence = {}
        fields = {name: name for name in (
            "Quotation_Reference", "Quotation_Validity_Period", "Quotation_Valid_From", "Quotation_Valid_Until",
            "Origin_Country", "Origin_City", "Origin_Port", "Destination_Country", "Destination_City",
            "Destination_Port", "Transport_Mode", "Service_Type", "Cargo_Type", "Fuel_Type", "Equipment_Type",
            "Container_Type", "Cargo_Weight", "Cargo_Volume", "Delivery_Time", "Transit_Time_Min_Days",
            "Transit_Time_Max_Days", "Estimated_Delivery_Date", "Quoted_Price", "Currency", "Pricing_Basis",
            "Fuel_Surcharge", "Additional_Charges", "Included_Charges", "Excluded_Charges", "Payment_Terms",
            "Incoterms", "Insurance_Included",
        )}
        is_demo = "synthetic" in str(raw.get("Record_Data_Status") or "").casefold()
        conflict_text = str(raw.get("Extraction_Notes") or "")
        conflict_fields = {name for name in fields if re.search(
            rf"(?:conflicting source values|conflict)[^\n]*\b{re.escape(name)}\b", conflict_text, re.I)}
        field_status = {}
        for field_name, column in fields.items():
            value = str(raw.get(column) or "").strip()
            score = confidence.get(field_name)
            try:
                score = float(score) if score is not None else None
            except (TypeError, ValueError):
                score = None
            field_evidence = evidence.get(field_name)
            has_evidence = bool(field_evidence)
            evidence_rows = field_evidence if isinstance(field_evidence, list) else []
            explicitly_not_applicable = any(
                isinstance(item, dict) and item.get("status") == "not_applicable" for item in evidence_rows)
            if not value:
                if explicitly_not_applicable:
                    status = "not_applicable"
                else:
                    status = "missing" if field_name in {*required_for_comparison, "Quoted_Price"} else "not_stated"
            elif is_demo:
                status = "demo"
            elif field_name in conflict_fields:
                status = "uncertain"
            elif score is not None and score < .70:
                status = "uncertain"
            elif score is not None and has_evidence:
                status = "high_confidence"
            elif score is not None:
                status = "uncertain"
            else:
                status = "unverified"
            field_status[field_name] = {"status": status,
                "confidence": round(score * 100) if score is not None else None,
                "evidence": field_evidence}
        quote = {
            **{field: str(raw.get(column) or "").strip() for field, column in fields.items()},
            "Supplier_ID": str(raw.get("Supplier_ID") or "").strip(),
            "Supplier_Name": str(raw.get("Supplier_Name") or "").strip(),
            "Quotation_ID": str(raw.get("Quotation_ID") or "").strip(),
            "Quotation_Received_Date": str(raw.get("Quotation_Received_Date") or "").strip(),
            "Quotation_Validity_Period": str(raw.get("Quotation_Validity_Period") or "").strip(),
            "Origin_Country": str(raw.get("Origin_Country") or "").strip(),
            "Origin_Port": str(raw.get("Origin_Port") or "").strip(),
            "Destination_Country": str(raw.get("Destination_Country") or "").strip(),
            "Destination_Port": str(raw.get("Destination_Port") or "").strip(),
            "Transit_Time_Min_Days": str(raw.get("Transit_Time_Min_Days") or "").strip(),
            "Transit_Time_Max_Days": str(raw.get("Transit_Time_Max_Days") or "").strip(),
            "Cargo_Weight": str(raw.get("Cargo_Weight") or "").strip(),
            "Cargo_Volume": str(raw.get("Cargo_Volume") or "").strip(),
            "Service_Type": str(raw.get("Service_Type") or "").strip(),
            "Record_Data_Status": str(raw.get("Record_Data_Status") or "Unverified quotation record"),
            "Data_Source": str(raw.get("Data_Source") or ""),
            "field_status": field_status,
            "evidence": evidence,
            "comparison_group": None,
        }
        valid_until = quote["Quotation_Valid_Until"]
        try:
            valid_date = date.fromisoformat(valid_until[:10]) if valid_until else None
        except ValueError:
            valid_date = None
        quote["availability_status"] = (
            "expired" if valid_date and valid_date < date.today()
            else "active" if valid_date
            else "unknown"
        )
        price = None
        try:
            price = Decimal(re.sub(r"[^0-9.\-]", "", quote["Quoted_Price"]))
        except Exception:  # noqa: BLE001 — malformed CSV values are shown but not ranked
            pass
        quote["_price"] = price
        transit = None
        transit_candidates = ("Transit_Time_Min_Days", "Delivery_Time", "Transit_Time_Max_Days")
        for candidate_name in transit_candidates:
            candidate = quote[candidate_name]
            if quote["field_status"][candidate_name]["status"] not in demo_statuses:
                continue
            match = re.search(r"\d+(?:\.\d+)?", candidate)
            if match:
                try:
                    transit = Decimal(match.group())
                    break
                except Exception:  # noqa: BLE001
                    pass
        quote["_transit"] = transit
        quotes.append(quote)
        equipment = quote["Equipment_Type"] or quote["Container_Type"]
        key_fields = (*(field for field in required_for_comparison if field != "Quotation_Valid_Until"),
                      "Origin_Port", "Destination_Port", "Service_Type", "Equipment_Type", "Container_Type",
                      "Cargo_Weight", "Cargo_Volume")
        key_values = [str(raw.get(field) or "").strip().casefold() for field in key_fields]
        key_is_grounded = all(quote["field_status"][field]["status"] in demo_statuses
                              for field in required_for_comparison)
        optional_match_fields = ("Origin_Port", "Destination_Port", "Service_Type", "Equipment_Type",
                                 "Container_Type", "Cargo_Weight", "Cargo_Volume")
        optional_details_grounded = all(
            not quote[field] or quote["field_status"][field]["status"] in demo_statuses
            for field in optional_match_fields)
        equipment_is_grounded = any(
            quote["field_status"][field]["status"] in demo_statuses and quote[field]
            for field in ("Equipment_Type", "Container_Type"))
        if key_is_grounded and optional_details_grounded and equipment and equipment_is_grounded \
                and quote["availability_status"] == "active":
            by_comparison_key.setdefault(tuple(key_values), []).append(quote)

    groups = []
    for index, (key, group_quotes) in enumerate(by_comparison_key.items(), start=1):
        if len(group_quotes) < 2:
            continue
        comparable_prices = [quote for quote in group_quotes if quote["_price"] is not None and
                             quote["field_status"]["Quoted_Price"]["status"] in demo_statuses]
        lowest = min(comparable_prices, key=lambda quote: quote["_price"]) if comparable_prices else None
        with_transit = [quote for quote in group_quotes if quote["_transit"] is not None and any(
            quote["field_status"][field]["status"] in demo_statuses
            for field in ("Transit_Time_Min_Days", "Delivery_Time", "Transit_Time_Max_Days"))]
        fastest = min(with_transit, key=lambda quote: quote["_transit"]) if with_transit else None
        payment_scores = []
        for quote in group_quotes:
            if quote["field_status"]["Payment_Terms"]["status"] not in demo_statuses:
                continue
            term = quote["Payment_Terms"].casefold()
            net = re.search(r"\bnet\s*(\d+)\b", term)
            if net:
                payment_scores.append((int(net.group(1)), quote))
            elif any(pattern in term for pattern in ("prepayment", "prepaid", "cash in advance", "advance payment")):
                payment_scores.append((0, quote))
        payment_flexible = max(payment_scores, key=lambda item: item[0])[1] if payment_scores else None
        documented_charge_counts = [
            (quote, len([part for part in re.split(r"[;|\n]+", quote["Additional_Charges"]) if part.strip()]))
            for quote in group_quotes if quote["field_status"]["Additional_Charges"]["status"] in {"high_confidence", "demo"}
        ]
        fewest_count = min((count for _, count in documented_charge_counts), default=None) \
            if len(documented_charge_counts) == len(group_quotes) else None
        group_id = f"master-compare-{index}"
        for quote in group_quotes:
            quote["comparison_group"] = group_id
        groups.append({
            "id": group_id,
            "route": f"{group_quotes[0]['Origin_City']} → {group_quotes[0]['Destination_City']}",
            "transport_mode": group_quotes[0]["Transport_Mode"],
            "cargo_type": group_quotes[0]["Cargo_Type"],
            "fuel_type": group_quotes[0]["Fuel_Type"],
            "equipment": group_quotes[0]["Equipment_Type"] or group_quotes[0]["Container_Type"],
            "currency": group_quotes[0]["Currency"],
            "pricing_basis": group_quotes[0]["Pricing_Basis"],
            "quotation_ids": [quote["Quotation_ID"] for quote in group_quotes],
            "lowest_price_quote_id": lowest["Quotation_ID"] if lowest else None,
            "fastest_quote_id": fastest["Quotation_ID"] if fastest else None,
            "most_flexible_payment_quote_id": payment_flexible["Quotation_ID"] if payment_flexible else None,
            "fewest_additional_charge_quote_ids": [quote["Quotation_ID"] for quote, count in documented_charge_counts
                                                    if fewest_count is not None and count == fewest_count],
            "fewest_additional_charge_count": fewest_count,
            "additional_charge_counts": {quote["Quotation_ID"]: len([part for part in re.split(r"[;|\n]+", quote["Additional_Charges"]) if part.strip()])
                                          for quote in group_quotes},
            "quotes": [{key: value for key, value in quote.items() if not key.startswith("_")}
                       for quote in group_quotes],
        })
    for quote in quotes:
        quote.pop("_price", None)
        quote.pop("_transit", None)
    return {"quotes": quotes, "groups": groups,
            "data_note": "Prices are compared only when route, mode, cargo, fuel, equipment, currency, and pricing basis match."}


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

    field_evidence = repos.evidence_for_quotation(str(quote.id))
    evidence_refs = [
        f"{e.field_name}: {e.source_filename}"
        + (f"[{e.page_or_sheet}]" if e.page_or_sheet else "")
        + f" “{e.snippet[:60]}”"
        for e in field_evidence
    ]
    comparison_fields = {
        "origin": quote.details.origin_city or quote.origin,
        "destination": quote.details.destination_city or quote.destination,
        "transport_type": quote.details.transport_type,
        "service_type": quote.details.service_type,
        "cargo_type": quote.details.cargo_type,
        "fuel_type": quote.details.fuel_type,
        "equipment_type": quote.details.equipment_type,
        "container_type": quote.details.container_type,
        "weight_kg": quote.details.weight_kg,
        "volume_m3": quote.details.volume_m3,
        "delivery_time": quote.details.delivery_time,
        "fuel_surcharge": quote.details.fuel_surcharge,
        "payment_terms": quote.details.payment_terms,
        "quotation_validity_period": quote.details.quotation_validity_period,
        "incoterms": quote.details.incoterms,
        "insurance_included": quote.insurance_included,
        "pricing_unit": quote.details.pricing_unit,
        "base_price": quote.base_price,
        "currency": quote.currency,
        "valid_until": quote.valid_until,
        "transit_days": quote.transit_days_max or quote.transit_days_min,
    }
    confidence: dict[str, float | None] = {}
    field_status: dict[str, str] = {}
    for field, value in comparison_fields.items():
        support = [item for item in field_evidence if item.field_name == field]
        scores = [item.confidence for item in support if item.confidence is not None]
        score = min(scores) if scores else None
        confidence[field] = score
        if value is None:
            field_status[field] = "missing" if field in {"base_price", "currency", "pricing_unit", "valid_until", "origin", "destination"} else "not_stated"
        elif any(item.validation_status.value == "needs_review" for item in support) or (score is not None and score < 0.70):
            field_status[field] = "uncertain"
        elif score is not None and support:
            field_status[field] = "high_confidence"
        else:
            field_status[field] = "unverified"
    charge_support = [item for item in field_evidence if item.field_name in {
        "charge", "fuel_surcharge", "included_charges", "excluded_charges"}]
    field_status["additional_charges"] = (
        "high_confidence" if charge_support and all(item.validation_status.value == "accepted" and
        item.confidence is not None and item.confidence >= .70 for item in charge_support) else
        "uncertain" if any(item.validation_status.value == "needs_review" for item in charge_support) else
        "not_stated")
    charge_scores = [item.confidence for item in charge_support if item.confidence is not None]
    confidence["additional_charges"] = min(charge_scores) if charge_scores else None
    additional_charges = []
    for charge in quote.known_additional_charges:
        amount = f"{charge.amount} {charge.currency or ''}".strip() if charge.amount is not None else "amount not stated"
        additional_charges.append(f"{charge.label}: {amount}" +
                                  (f" / {charge.pricing_unit}" if charge.pricing_unit else ""))

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
        origin=quote.details.origin_city or quote.origin,
        destination=quote.details.destination_city or quote.destination,
        origin_country=quote.details.origin_country,
        origin_city=quote.details.origin_city,
        origin_port=quote.details.origin_port,
        destination_country=quote.details.destination_country,
        destination_city=quote.details.destination_city,
        destination_port=quote.details.destination_port,
        transport_mode=quote.details.transport_type,
        service_type=quote.details.service_type,
        cargo_type=quote.details.cargo_type,
        fuel_type=quote.details.fuel_type,
        equipment_type=quote.details.equipment_type,
        container_type=quote.details.container_type,
        cargo_weight=str(quote.details.weight_kg) if quote.details.weight_kg is not None else None,
        cargo_volume=str(quote.details.volume_m3) if quote.details.volume_m3 is not None else None,
        delivery_time=quote.details.delivery_time,
        estimated_delivery_date=quote.details.estimated_delivery_date,
        fuel_surcharge=quote.details.fuel_surcharge,
        additional_charges=additional_charges,
        included_charges=quote.details.included_charges,
        excluded_charges=quote.details.excluded_charges,
        payment_terms=quote.details.payment_terms,
        quotation_validity_period=quote.details.quotation_validity_period,
        incoterms=quote.details.incoterms,
        insurance_included=bool(quote.insurance_included) if quote.insurance_included is not None else None,
        field_confidence=confidence,
        field_status=field_status,
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
        fastest = min((row for row in eligible if row.transit_days is not None),
                      key=lambda row: row.transit_days, default=None)
        if fastest:
            report.recommendation_basis.append(
                f"Fastest documented transit: {fastest.carrier_name or fastest.quote_reference} at up to {fastest.transit_days} days.")
        payment_scores = []
        for row in eligible:
            term = (row.payment_terms or "").casefold()
            net = re.search(r"\bnet\s*(\d+)\b", term)
            if net:
                payment_scores.append((int(net.group(1)), row))
            elif any(value in term for value in ("prepayment", "prepaid", "cash in advance", "advance payment")):
                payment_scores.append((0, row))
        if payment_scores:
            most_flexible = max(payment_scores, key=lambda item: item[0])[1]
            report.recommendation_basis.append(
                f"Most flexible recognized payment term: {most_flexible.carrier_name or most_flexible.quote_reference} ({most_flexible.payment_terms}).")
        if eligible and all(row.field_status.get("additional_charges") == "high_confidence" for row in eligible):
            fewest = min(len(row.additional_charges) for row in eligible)
            winners = [row.carrier_name or row.quote_reference for row in eligible
                       if len(row.additional_charges) == fewest]
            report.recommendation_basis.append(
                "Fewest documented additional charges: " + ", ".join(winners) + f" ({fewest}).")
        report.recommendation_basis.append(
            "Best overall match is the lowest known total among quotes that pass the selected shipment, route, scope, currency, and pricing-basis checks; use the service and terms tabs to review trade-offs.")
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
    headers = ["carrier", "status", "origin", "destination", "mode", "cargo", "fuel", "equipment",
               "currency", "base_price", "pricing_basis", "known_extras", "known_subtotal",
               f"in_{report.target_currency}", "in_USD", "scope", "delivery_time", "transit",
               "fuel_surcharge", "payment_terms", "valid_until", "incoterms", "insurance",
               "critical_gaps", "uncertain_charges"]
    body = []
    for r in report.rows:
        body.append([
            r.carrier_name or "-", str(r.comparison_status), r.origin or "-", r.destination or "-",
            r.transport_mode or "-", r.cargo_type or "-", r.fuel_type or "-",
            r.equipment_type or r.container_type or "-", r.currency or "-",
            "" if r.base_price is None else str(r.base_price),
            r.pricing_unit or "-",
            "" if r.known_additional_total is None else str(r.known_additional_total),
            "" if r.known_subtotal is None else str(r.known_subtotal),
            "" if r.converted_subtotal is None else str(r.converted_subtotal),
            "" if r.usd_subtotal is None else str(r.usd_subtotal),
            r.delivery_scope, r.delivery_time or "-", r.transit_time,
            r.fuel_surcharge or "-", r.payment_terms or "-",
            "" if r.valid_until is None else r.valid_until.isoformat(),
            r.incoterms or "-", "" if r.insurance_included is None else str(r.insurance_included),
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

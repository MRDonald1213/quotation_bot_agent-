from __future__ import annotations

import re
from datetime import date

from ..config import Settings
from ..domain.enums import (
    ComparisonReadiness,
    ComparisonStatus,
    GapSeverity,
    GapType,
)
from ..domain.models import (
    ClarificationQuestion,
    Gap,
    GapAnalysisResult,
    GapItem,
    Quotation,
    Shipment,
)
from ..db.repositories import Repos

# The cross-carrier comparison schema: fields required for a fair comparison.
COMPARISON_SCHEMA: list[tuple[str, GapSeverity, str]] = [
    ("currency", GapSeverity.critical, "Prices cannot be compared across currencies without an explicit FX rate."),
    ("base_price", GapSeverity.critical, "The headline price drives the whole comparison."),
    ("delivery_scope", GapSeverity.critical, "Port-to-port and door-to-door quotes are not comparable as if equivalent."),
    ("valid_until", GapSeverity.important, "An expired or undated quote cannot be relied on for decision timing."),
    ("transit_days", GapSeverity.important, "Transit time affects inventory and delivery promises."),
    ("insurance_included", GapSeverity.important, "Unknown insurance coverage hides a possible extra cost."),
    ("customs_included", GapSeverity.important, "Unknown customs handling hides a possible extra cost."),
    ("quote_reference", GapSeverity.informational, "A quote reference helps both sides track the offer."),
]

# Only these fields belong in a supplier email: they describe the offer itself. Internal
# master-data columns (ratings, compliance, payment terms) are never asked of a supplier.
ASKABLE_FIELDS: frozenset[str] = frozenset({
    "currency", "base_price", "charges", "delivery_scope", "valid_until", "transit_days",
    "insurance_included", "customs_included", "origin", "destination",
    "included_services", "excluded_services",
})

ASKABLE_QUESTIONS: dict[str, str] = {
    "currency": "Which currency is this price in?",
    "carrier_name": "What supplier or carrier name should we use for this quotation?",
    "pricing_unit": "Is this price per shipment, per kilogram, per hour, or another pricing unit?",
    "base_price": "What is the base freight price, and what does it include?",
    "charges": "For each extra charge not included in the base price, what is the amount and currency?",
    "delivery_scope": "Does this price cover delivery to the final address, or only to the port?",
    "valid_until": "Until what date (YYYY-MM-DD) is this quotation valid?",
    "transit_days": "What is the expected transit time in days, minimum and maximum?",
    "insurance_included": "Is cargo insurance included? If not, what does it cost?",
    "customs_included": "Are customs clearance and handling included in this price?",
    "origin": "Which origin does this price cover?",
    "destination": "Which destination does this price cover?",
    "included_services": "Which services are included in the quoted price?",
    "excluded_services": "Which services are excluded, and what do they cost separately?",
    "quote_reference": "What reference number should we quote when we accept this offer?",
}


def askable_question(field_name: str) -> str:
    """Plain business-English question for a supplier-answerable field."""
    return ASKABLE_QUESTIONS.get(
        field_name, f"Could you confirm the {field_name.replace('_', ' ')} for this quotation?")


_TRANSIT_FIELDS = {"transit_days_min", "transit_days_max", "transit_days"}


def canonical_field(name: str) -> str:
    """Collapse the storage names of one comparison field onto its schema name."""
    if name in _TRANSIT_FIELDS:
        return "transit_days"
    if name == "known_additional_charges" or re.match(r"^(?:charges|known_additional_charges)[:.\[]", name):
        return "charges"
    return name


def detect_gaps_deterministic(shipment: Shipment, quote: Quotation) -> list[GapItem]:
    """Workflow A step 7: compare extracted fields against shipment requirements and the
    cross-carrier comparison schema. Pure rule code — no LLM arithmetic or judgement."""
    gaps: list[GapItem] = []

    values = {
        "currency": quote.currency,
        "base_price": quote.base_price,
        "delivery_scope": quote.delivery_scope,
        "valid_until": quote.valid_until,
        "transit_days": quote.transit_days_min if quote.transit_days_min is not None else quote.transit_days_max,
        "insurance_included": quote.insurance_included,
        "customs_included": quote.customs_included,
        "quote_reference": quote.quote_reference,
    }
    for field, severity, why in COMPARISON_SCHEMA:
        if values[field] is None or (field == "delivery_scope" and str(values[field]) == "unknown"):
            gaps.append(GapItem(field_name=field, gap_type=GapType.missing, severity=severity,
                                description=f"{field} is not known: {why}"))

    # scope mismatch against the shipment requirement
    if str(shipment.required_delivery_scope) != "unknown" and quote.delivery_scope is not None \
            and quote.delivery_scope != shipment.required_delivery_scope:
        gaps.append(GapItem(
            field_name="delivery_scope", gap_type=GapType.scope_mismatch, severity=GapSeverity.critical,
            description=(f"quote scope {quote.delivery_scope} differs from required "
                         f"{shipment.required_delivery_scope}; not directly comparable")))

    # currency mismatch against shipment preference
    if quote.currency and quote.currency != shipment.target_currency:
        gaps.append(GapItem(
            field_name="currency", gap_type=GapType.missing, severity=GapSeverity.important,
            description=(f"quoted in {quote.currency} but shipment targets {shipment.target_currency}; "
                         "comparison needs an explicit FX rate and date (none configured)")))

    if quote.valid_until is not None and quote.valid_until < date.today():
        gaps.append(GapItem(field_name="valid_until", gap_type=GapType.expired,
                            severity=GapSeverity.critical,
                            description=f"quote expired on {quote.valid_until}"))

    for ch in quote.known_additional_charges:
        if ch.included_in_base_price is True:
            continue
        if ch.mandatory is not False and (ch.amount is None or ch.included_in_base_price is None):
            gaps.append(GapItem(
                field_name="charges", gap_type=GapType.ambiguous, severity=GapSeverity.critical,
                description=(f"mandatory charge {ch.label!r} is unpriced or its applicability is unclear; "
                             "it is not treated as zero")))
        elif ch.amount is not None and ch.included_in_base_price is False and (
                ch.currency is None or ch.currency != quote.currency):
            gaps.append(GapItem(
                field_name="charges", gap_type=GapType.ambiguous, severity=GapSeverity.critical,
                description=f"charge {ch.label!r} has unknown or different currency; no FX conversion is assumed"))

    if quote.base_price is not None and quote.total_known_cost is None:
        gaps.append(GapItem(
            field_name="total_known_cost", gap_type=GapType.ambiguous, severity=GapSeverity.important,
            description="known subtotal blocked by unresolved charges; shown as incomplete"))

    return gaps


def template_questions(shipment: Shipment, quote: Quotation, gaps: list[GapItem],
                       carrier_name: str | None, repos: Repos,
                       evidence_snippets: dict[str, str]) -> list[ClarificationQuestion]:
    questions: list[ClarificationQuestion] = []
    carrier = repos.get_carrier(str(quote.carrier_id)) if quote.carrier_id else None
    for gap in gaps:
        questions.append(ClarificationQuestion(
            field_name=gap.field_name,
            why_it_matters=gap.description,
            question_text=askable_question(gap.field_name),
            priority=gap.severity,
            source_context=evidence_snippets.get(gap.field_name, "not found"),
            target_carrier_id=carrier.id if carrier else None,
            target_carrier_name=carrier.name if carrier else carrier_name,
        ))
    return questions


def readiness(gaps: list[GapItem]) -> ComparisonReadiness:
    critical = [g for g in gaps if g.severity == GapSeverity.critical]
    if not gaps:
        return ComparisonReadiness.ready
    if critical:
        return ComparisonReadiness.not_ready
    return ComparisonReadiness.partially_ready


def summarize(quote: Quotation, gaps: list[GapItem]) -> str:
    n_crit = sum(1 for g in gaps if g.severity == GapSeverity.critical)
    if not gaps:
        return "All comparison-required fields are present; quote is comparable."
    return (f"{len(gaps)} gap(s) detected ({n_crit} critical). "
            f"Status: {quote.comparison_status}. Critical gaps must be clarified before ranking.")


def analyze_gaps_for_quote(settings: Settings, shipment: Shipment, quote: Quotation,
                           repos: Repos, evidence_snippets: dict[str, str],
                           carrier_name: str | None) -> GapAnalysisResult:
    gaps = detect_gaps_deterministic(shipment, quote)
    questions = template_questions(shipment, quote, gaps, carrier_name, repos, evidence_snippets)
    return GapAnalysisResult(
        gaps=gaps,
        clarification_questions=questions,
        comparison_readiness=readiness(gaps),
        summary=summarize(quote, gaps),
    )


def comparison_status_after_gaps(quote: Quotation, gaps: list[GapItem]) -> ComparisonStatus:
    if any(g.gap_type == GapType.expired for g in gaps):
        return ComparisonStatus.expired
    if quote.comparison_status == ComparisonStatus.needs_review:
        return ComparisonStatus.needs_review
    if any(g.severity == GapSeverity.critical for g in gaps):
        return ComparisonStatus.incomplete
    return ComparisonStatus.comparable


def shipment_gap_report(settings: Settings, id_or_reference: str
                        ) -> tuple[Shipment, list[tuple[Quotation, GapAnalysisResult]]]:
    """Read-only gap analysis over every quote of a shipment (workflow A step 7 view)."""
    from ..domain.enums import ValidationStatus
    from .common import with_session

    with with_session(settings) as repos:
        shipment = repos.get_shipment(id_or_reference)
        results: list[tuple[Quotation, GapAnalysisResult]] = []
        for quote in repos.list_quotations(str(shipment.id)):
            snippets = {
                e.field_name: e.snippet
                for e in repos.evidence_for_quotation(str(quote.id))
                if e.validation_status == ValidationStatus.accepted
            }
            analysis = analyze_gaps_for_quote(settings, shipment, quote, repos, snippets, None)
            results.append((quote, analysis))
        return shipment, results


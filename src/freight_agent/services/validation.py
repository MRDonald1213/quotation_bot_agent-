from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal

import pycountry

from ..domain.enums import DeliveryScope, ValidationStatus
from ..domain.exceptions import ValidationFailureError
from ..domain.money import normalize_currency, parse_money
from ..domain.models import Charge, EvidenceItem, QuoteExtraction

WS = re.compile(r"\s+")

# Word auto-corrects quotes/dashes and the model answers in plain ASCII;
# grounding must treat them as equal or real .docx files never match.
_PUNCT_FOLD = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2026": "...", "\u00a0": " ",
})

MATERIAL_FIELDS = [
    "carrier_name", "currency", "base_price", "origin", "destination",
    "transit_days_min", "transit_days_max", "valid_until", "delivery_scope",
    "insurance_included", "customs_included", "quote_reference",
    "included_services", "excluded_services", "origin_country", "destination_country",
    "cargo_description", "weight_kg", "volume_m3",
]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT_FOLD)
    # redaction masks: "*** *** " and "*******" are the same token to model and source
    text = re.sub(r"\s*\*+\s*", "*", text)
    return WS.sub(" ", text).strip().lower()


def snippet_grounded(snippet: str, raw_text: str) -> bool:
    """Evidence must be a verbatim (whitespace/case-insensitive) fragment of the source."""
    if not snippet.strip():
        return False
    needle = _norm(snippet)
    return needle in _norm(raw_text)


def _amount_grounded(amount: Decimal, snippet: str) -> bool:
    return any(Decimal(token.replace(",", "")) == amount
               for token in re.findall(r"(?<![\w.,])[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?![\w.,])", snippet))


_SCOPE_ALIASES = {
    "port to port": "port_to_port", "door to door": "door_to_door",
    "door to port": "door_to_port", "port to door": "port_to_door",
    "dtd": "door_to_door", "ptp": "port_to_port", "dtp": "door_to_port",
    "ptd": "port_to_door", "unknown": "unknown",
}


def normalize_scope(value: str) -> str | None:
    """Map free-form carrier wording onto the DeliveryScope enum values."""
    key = re.sub(r"[\s_]+", " ", value.strip().lower().replace("-", " "))
    key = re.sub(r"\s+", " ", key)
    if key in _SCOPE_ALIASES:
        return _SCOPE_ALIASES[key]
    if key.replace(" ", "_") in {s.value for s in DeliveryScope}:
        return key.replace(" ", "_")
    return None


def normalize_country(value: str) -> str:
    try:
        return pycountry.countries.lookup(value.strip()).alpha_2
    except LookupError:
        raise ValidationFailureError(f"Unknown country: {value!r}; supply a country name or ISO alpha-2 code.") from None


def country_from_location(location: str) -> str | None:
    matches: list[tuple[int, int, str]] = []
    for country in pycountry.countries:
        names = {country.name, getattr(country, "official_name", country.name),
                 getattr(country, "common_name", country.name)}
        for name in names:
            for match in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", location, re.I):
                matches.append((match.start(), match.end(), country.alpha_2))
        for code in (country.alpha_2, country.alpha_3):
            for match in re.finditer(r"(?<!\w)" + code + r"(?!\w)", location):
                matches.append((match.start(), match.end(), country.alpha_2))
    retained: list[tuple[int, int, str]] = []
    for match in sorted(matches, key=lambda m: m[1] - m[0], reverse=True):
        if not any(start <= match[0] and end >= match[1] for start, end, _ in retained):
            retained.append(match)
    countries = {country for _, _, country in retained}
    return next(iter(countries)) if len(countries) == 1 else None


def compute_total_known_cost(base_price: Decimal | None, currency: str | None,
                             charges: list[Charge]) -> tuple[Decimal | None, list[str]]:
    """Deterministic money — the LLM never calculates (spec §2, §10).

    Adds only charges that are: numeric, confirmed NOT included in base price,
    and in the quote currency. Any mandatory charge with unknown amount or foreign
    currency blocks a definitive total.
    """
    notes: list[str] = []
    if base_price is None:
        return None, ["base price unknown; no total computed"]
    if currency is None:
        return None, ["currency unknown; no total computed"]
    total = base_price
    blocking = False
    for ch in charges:
        if ch.included_in_base_price is True:
            continue
        if ch.included_in_base_price is None or ch.amount is None:
            if ch.mandatory is not False:
                blocking = True
                notes.append(f"charge {ch.label!r} has unknown amount or inclusion — unresolved, not zero")
            continue
        if ch.currency is None or ch.currency != currency:
            blocking = True
            notes.append(f"charge {ch.label!r} currency is unknown or differs from {currency}; not summed (no FX)")
            continue
        total += ch.amount
    return (None if blocking else total), notes


def validate_extraction(extraction: QuoteExtraction, raw_text: str,
                        source_filename: str) -> tuple[QuoteExtraction, list[EvidenceItem], dict[str, ValidationStatus], list[str]]:
    """Domain validator for workflow A step 6: currency codes, monetary values, dates,
    impossible values, and required evidence grounding. LLM output is untrusted until
    this passes."""
    issues: list[str] = []
    review_fields = set(extraction.fields_requiring_review)

    currency: str | None = None
    if extraction.currency:
        try:
            currency = normalize_currency(extraction.currency)
        except ValidationFailureError as exc:
            issues.append(str(exc))
            review_fields.add("currency")

    base = parse_money(extraction.base_price, field="base_price") if extraction.base_price is not None else None
    if extraction.base_price is None:
        review_fields.add("base_price")

    charges: list[Charge] = []
    for ch in extraction.charges:
        try:
            credit = bool(re.search(r"\b(discount|credit)\b", ch.label, re.I)
                          and re.search(r"\b(discount|credit)\b", ch.evidence_snippet or "", re.I))
            amount = parse_money(ch.amount, field=f"charge {ch.label}", allow_negative=credit)
        except ValidationFailureError as exc:
            issues.append(str(exc))
            amount = None
            review_fields.add(f"charges:{ch.label}")
        try:
            ch_currency = normalize_currency(ch.currency) if ch.currency else None
        except ValidationFailureError as exc:
            issues.append(str(exc))
            ch_currency = None
            review_fields.add(f"charges:{ch.label}")
        if not ch.evidence_snippet or not snippet_grounded(ch.evidence_snippet, raw_text) or (
                amount is not None and not _amount_grounded(amount, ch.evidence_snippet)):
            issues.append(f"charge {ch.label!r} lacks grounded amount evidence")
            review_fields.add(f"charges:{ch.label}")
        charges.append(ch.model_copy(update={"amount": amount, "currency": ch_currency}))

    if (extraction.transit_days_min is not None and extraction.transit_days_max is not None
            and extraction.transit_days_min > extraction.transit_days_max):
        issues.append("transit_days_min > transit_days_max")
        review_fields.update({"transit_days_min", "transit_days_max"})
    for tf in ("transit_days_min", "transit_days_max"):
        if getattr(extraction, tf) is not None and getattr(extraction, tf) < 0:
            issues.append(f"{tf} negative")
            review_fields.add(tf)

    if extraction.valid_until is not None and extraction.valid_until < date.today():
        issues.append("quote validity date already in the past")

    # Evidence grounding
    evidence_by_field: dict[str, EvidenceItem] = {}
    grounded: list[EvidenceItem] = []
    for ev in extraction.field_evidence:
        if snippet_grounded(ev.snippet, raw_text):
            evidence_by_field[ev.field_name] = ev
            grounded.append(ev)
        else:
            issues.append(f"evidence for {ev.field_name!r} not found verbatim in source — ungrounded")
            review_fields.add(ev.field_name)

    if base is not None and "base_price" in evidence_by_field and not _amount_grounded(base, evidence_by_field["base_price"].snippet):
        issues.append("base_price amount is not present in its evidence snippet")
        review_fields.add("base_price")

    # a route value copied verbatim from the document is its own evidence even when
    # the model omitted a separate snippet for it
    self_grounded = {
        f for f in ("origin", "destination")
        if getattr(extraction, f) and snippet_grounded(getattr(extraction, f), raw_text)
    }

    for field in MATERIAL_FIELDS:
        value = getattr(extraction, field, None)
        if value is None or value == []:
            continue
        if field not in evidence_by_field and field not in self_grounded:
            review_fields.add(field)
            issues.append(f"field {field!r} extracted without source evidence — flagged for review")

    route_values: dict[str, object] = {}
    for field in ("origin", "destination"):
        value = getattr(extraction, field)
        evidence = evidence_by_field.get(field)
        if value and field not in self_grounded and (evidence is None or not snippet_grounded(value, evidence.snippet)):
            review_fields.add(field)
            issues.append(f"{field} value is not present in its route evidence")
        elif value and field in self_grounded:
            review_fields.discard(field)
            issues.append(f"{field} accepted: value appears verbatim in the document")
    for field in ("origin_country", "destination_country"):
        value = getattr(extraction, field)
        evidence = evidence_by_field.get(field)
        if value:
            try:
                code = normalize_country(value)
            except ValidationFailureError as exc:
                issues.append(str(exc))
                code = None
            if (code is None or evidence is None or field in review_fields
                    or country_from_location(evidence.snippet) != code):
                review_fields.add(field)
                route_values[field] = None
                issues.append(f"{field} lacks an explicitly stated, unambiguous country")
            else:
                route_values[field] = code
    for field in ("weight_kg", "volume_m3"):
        value = getattr(extraction, field)
        evidence = evidence_by_field.get(field)
        if value is not None and (not value.is_finite() or value <= 0 or evidence is None
                                  or not _amount_grounded(value, evidence.snippet)):
            route_values[field] = None
            review_fields.add(field)
            issues.append(f"{field} must be positive and supported by numeric source evidence")

    if extraction.delivery_scope:
        scope = normalize_scope(extraction.delivery_scope)
        if scope is None:
            review_fields.add("delivery_scope")
            issues.append(f"delivery_scope {extraction.delivery_scope!r} is not a recognized scope")
            route_values["delivery_scope"] = None
        else:
            route_values["delivery_scope"] = scope

    cleaned = extraction.model_copy(update={
        "currency": currency,
        "base_price": base,
        "charges": charges,
        **route_values,
        "fields_requiring_review": sorted(review_fields),
    })
    statuses: dict[str, ValidationStatus] = {
        f: (ValidationStatus.needs_review if f in review_fields else ValidationStatus.accepted)
        for f in MATERIAL_FIELDS
    }
    return cleaned, grounded, statuses, issues

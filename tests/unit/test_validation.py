from __future__ import annotations

from decimal import Decimal

from freight_agent.domain.enums import ValidationStatus
from freight_agent.domain.models import EvidenceItem, QuoteExtraction
from freight_agent.services.validation import snippet_grounded, validate_extraction

RAW = "Base ocean freight: 3,200 EUR per 40ft container, valid until 2026-11-15."


def _extraction(**overrides) -> QuoteExtraction:
    base = dict(
        carrier_name="Baltic Lines",
        currency="EUR",
        base_price=Decimal("3200"),
        valid_until="2026-11-15",
        field_evidence=[
            EvidenceItem(field_name="base_price", snippet="Base ocean freight: 3,200 EUR"),
            EvidenceItem(field_name="currency", snippet="3,200 EUR"),
            EvidenceItem(field_name="valid_until", snippet="valid until 2026-11-15"),
        ],
    )
    base.update(overrides)
    return QuoteExtraction.model_validate(base)


def test_grounded_extraction_accepts_fields_with_evidence():
    cleaned, evidence, statuses, issues = validate_extraction(_extraction(), RAW, "q.txt")
    assert cleaned.base_price == Decimal("3200")
    assert "base_price" not in cleaned.fields_requiring_review
    assert statuses["base_price"] == ValidationStatus.accepted


def test_field_without_evidence_is_flagged_for_review():
    ex = _extraction(field_evidence=[
        EvidenceItem(field_name="currency", snippet="3,200 EUR"),
        EvidenceItem(field_name="valid_until", snippet="valid until 2026-11-15")])
    cleaned, _, statuses, issues = validate_extraction(ex, RAW, "q.txt")
    assert "base_price" in cleaned.fields_requiring_review
    assert statuses["base_price"] == ValidationStatus.needs_review


def test_ungrounded_snippet_rejected():
    ex = _extraction(field_evidence=[
        EvidenceItem(field_name="base_price", snippet="total price 9999 EUR forever")])
    cleaned, evidence, _, issues = validate_extraction(ex, RAW, "q.txt")
    assert not snippet_grounded("total price 9999 EUR forever", RAW)
    assert "base_price" in cleaned.fields_requiring_review
    assert all(e.snippet != "total price 9999 EUR forever" for e in evidence)


def test_negative_transit_and_min_gt_max_flagged():
    ex = _extraction(transit_days_min=10, transit_days_max=4,
                     field_evidence=[EvidenceItem(field_name="transit_days_min",
                                                  snippet="3,200 EUR")])
    cleaned, _, _, issues = validate_extraction(ex, RAW, "q.txt")
    assert any("transit_days_min" in i for i in issues)


def test_invalid_currency_goes_to_review_not_crash():
    ex = _extraction(currency="Dollars")
    cleaned, _, _, issues = validate_extraction(ex, RAW, "q.txt")
    assert "currency" in cleaned.fields_requiring_review or cleaned.currency is None

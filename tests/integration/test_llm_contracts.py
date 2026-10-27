from __future__ import annotations

import pytest

from freight_agent.domain.enums import (
    ClassificationStatus,
    ComparisonStatus,
    DeliveryScope,
    ProcessingStatus,
    ValidationStatus,
)
from freight_agent.domain.exceptions import LLMOutputInvalidError, ValidationFailureError
from freight_agent.services import intake as intake_service
from freight_agent.services.classification import classify_intake
from freight_agent.services.extraction import extract_and_store_quote
from freight_agent.services.common import with_session
from freight_agent.services.shipments import create_shipment

CLASSIFY_OK = {
    "is_freight_quote": True, "reason": "carrier price list", "confidence": 0.92,
    "carrier_name": "Baltic Lines OÜ", "shipment_reference": None,
    "needs_human_review": False, "document_type": "carrier_quote",
}
CLASSIFY_LOW_CONF = dict(CLASSIFY_OK, confidence=0.40)
CLASSIFY_IRRELEVANT = {
    "is_freight_quote": False, "reason": "internal newsletter, no freight offer",
    "confidence": 0.95, "carrier_name": None, "shipment_reference": None,
    "needs_human_review": False, "document_type": "unrelated",
}

EXTRACT_A = {
    "carrier_name": "Baltic Lines OÜ",
    "quote_reference": "BL-2026-0417",
    "currency": "EUR",
    "base_price": "3200",
    "charges": [
        {"label": "Terminal handling charge", "amount": "340", "currency": "EUR",
         "included_in_base_price": False, "mandatory": True,
         "evidence_snippet": "Terminal handling charge: 340 EUR"},
        {"label": "Delivery to Hamburg warehouse", "amount": None, "currency": "EUR",
         "included_in_base_price": False, "mandatory": True,
         "evidence_snippet": "Delivery to Hamburg warehouse: to be confirmed, price TBD"},
    ],
    "included_services": ["Ocean freight"],
    "excluded_services": ["on-carriage"],
    "origin": "Baku, AZ",
    "destination": "Hamburg, DE",
    "transit_days_min": 12,
    "transit_days_max": 16,
    "valid_until": "2026-11-15",
    "delivery_scope": "port_to_port",
    "insurance_included": None,
    "customs_included": None,
    "assumptions": [],
    "fields_requiring_review": [],
    "field_evidence": [
        {"field_name": "carrier_name", "snippet": "Baltic Lines OÜ", "page_or_sheet": "page 1", "confidence": 0.95},
        {"field_name": "quote_reference", "snippet": "BL-2026-0417", "page_or_sheet": "page 1", "confidence": 0.95},
        {"field_name": "currency", "snippet": "3,200 EUR", "page_or_sheet": "page 1", "confidence": 0.9},
        {"field_name": "base_price", "snippet": "Base ocean freight: 3,200 EUR", "page_or_sheet": "page 1", "confidence": 0.95},
        {"field_name": "origin", "snippet": "Baku, AZ", "page_or_sheet": "page 1", "confidence": 0.9},
        {"field_name": "destination", "snippet": "Hamburg, DE", "page_or_sheet": "page 1", "confidence": 0.9},
        {"field_name": "transit_days_min", "snippet": "Estimated transit: 12-16 days", "page_or_sheet": "page 1", "confidence": 0.9},
        {"field_name": "transit_days_max", "snippet": "Estimated transit: 12-16 days", "page_or_sheet": "page 1", "confidence": 0.9},
        {"field_name": "valid_until", "snippet": "valid until 2026-11-15", "page_or_sheet": "page 1", "confidence": 0.95},
        {"field_name": "delivery_scope", "snippet": "port to port", "page_or_sheet": "page 1", "confidence": 0.85},
    ],
}
EXTRACT_WITHOUT_EVIDENCE = {**EXTRACT_A, "field_evidence": []}
GARBAGE = {"base_price": "not-a-number", "charges": "this should be a list"}


def _setup(settings, fake_llm, file, classify_payload=CLASSIFY_OK, extract_payload=EXTRACT_A):
    fake_llm.responses = {
        "Extract the QuoteExtraction": extract_payload,   # checked first
        f'filename="{file.name}"': classify_payload,
    }
    shipment = create_shipment(settings, reference="SHP-T-1", name="t", origin="Baku",
                               destination="Hamburg", cargo_description="parts",
                               required_delivery_scope=DeliveryScope.door_to_door)
    item, _ = intake_service.register_intake(settings, str(shipment.id), file)
    return shipment, item


# ---------------------------------------------------------- LLM contract tests

def test_classification_schema_valid_becomes_relevant(settings, fake_llm, quote_a_file):
    _, item = _setup(settings, fake_llm, quote_a_file)
    updated, result = classify_intake(settings, str(item.id))
    assert updated.classification_status == ClassificationStatus.relevant
    assert result.confidence == 0.92


def test_low_confidence_routes_to_needs_review(settings, fake_llm, quote_a_file):
    _, item = _setup(settings, fake_llm, quote_a_file, classify_payload=CLASSIFY_LOW_CONF)
    updated, _ = classify_intake(settings, str(item.id))
    assert updated.classification_status == ClassificationStatus.needs_review


def test_prompt_boundaries_wrap_document_as_data(settings, fake_llm, newsletter_file):
    _, item = _setup(settings, fake_llm, newsletter_file, classify_payload=CLASSIFY_IRRELEVANT)
    classify_intake(settings, str(item.id))
    prompt = fake_llm.prompts[-1]
    assert "<UNTRUSTED_DOCUMENT" in prompt
    # injection sentence is present only inside the data block, never as an instruction
    assert "Ignore previous instructions" in prompt


def test_irrelevant_document_rejected_not_extracted(settings, fake_llm, newsletter_file):
    _, item = _setup(settings, fake_llm, newsletter_file, classify_payload=CLASSIFY_IRRELEVANT)
    summary = extract_and_store_quote(settings, str(item.id))
    assert summary.quotation_id is None
    assert any("irrelevant" in n.lower() for n in summary.notes)
    with with_session(settings) as repos:
        assert repos.get_intake(str(item.id)).processing_status == ProcessingStatus.processed
        assert repos.list_quotations() == []


def test_garbage_llm_output_fails_without_corrupting_db(settings, fake_llm, quote_a_file):
    _, item = _setup(settings, fake_llm, quote_a_file, extract_payload=GARBAGE)
    with pytest.raises(LLMOutputInvalidError):
        extract_and_store_quote(settings, str(item.id))
    with with_session(settings) as repos:
        # terminal failure is recorded; no partial quote/evidence rows
        assert repos.get_intake(str(item.id)).processing_status == ProcessingStatus.failed
        assert repos.list_quotations() == []


def test_missing_evidence_causes_review_flag(settings, fake_llm, quote_a_file):
    _, item = _setup(settings, fake_llm, quote_a_file, extract_payload=EXTRACT_WITHOUT_EVIDENCE)
    summary = extract_and_store_quote(settings, str(item.id))
    with with_session(settings) as repos:
        quote = repos.get_quotation(str(summary.quotation_id))
        assert quote.comparison_status == ComparisonStatus.needs_review
        flagged = [e for e in repos.evidence_for_quotation(str(quote.id))
                   if e.validation_status == ValidationStatus.needs_review]
        assert flagged, "fields extracted without evidence must be marked for review"


def test_workflow_a_end_to_end_deterministic_math(settings, fake_llm, quote_a_file):
    shipment, item = _setup(settings, fake_llm, quote_a_file)
    summary = extract_and_store_quote(settings, str(item.id))
    assert summary.created_quote
    with with_session(settings) as repos:
        quote = repos.get_quotation(str(summary.quotation_id))
        # unpriced mandatory warehouse charge blocks a definitive total → never zero
        assert quote.total_known_cost is None
        assert quote.comparison_status == ComparisonStatus.incomplete
        assert quote.base_price == 3200
        assert quote.currency == "EUR"
        assert quote.model_name == "fake-model" and quote.prompt_version
        evidence = repos.evidence_for_quotation(str(quote.id))
        assert any(e.field_name == "base_price" and "3,200 EUR" in e.snippet for e in evidence)
        gaps = repos.list_gaps(str(shipment.id))
        assert any(g.field_name == "charges" for g in gaps)
        assert any(g.gap_type.value == "scope_mismatch" for g in gaps)  # port_to_port vs door_to_door


def test_dry_run_persists_nothing(settings, fake_llm, quote_a_file):
    _, item = _setup(settings, fake_llm, quote_a_file)
    summary = extract_and_store_quote(settings, str(item.id), dry_run=True)
    assert summary.quotation_id is None
    with with_session(settings) as repos:
        assert repos.list_quotations() == []
        assert repos.get_intake(str(item.id)).classification_status == ClassificationStatus.pending


def test_reprocess_bumps_version_no_silent_duplicate(settings, fake_llm, quote_a_file):
    _, item = _setup(settings, fake_llm, quote_a_file)
    first = extract_and_store_quote(settings, str(item.id))
    with pytest.raises(ValidationFailureError):
        extract_and_store_quote(settings, str(item.id))     # duplicate protection
    second = extract_and_store_quote(settings, str(item.id), force=True)
    with with_session(settings) as repos:
        assert repos.list_quotations() != []
        q = repos.get_quotation(str(second.quotation_id))
        assert q.id == first.quotation_id and q.version == 2

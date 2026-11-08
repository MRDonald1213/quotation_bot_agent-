from decimal import Decimal
from uuid import uuid4

from freight_agent.domain.models import IntakeItem, Quotation, QuoteExtraction, FieldEvidence
from freight_agent.domain.enums import ValidationStatus
from freight_agent.services.replies import _merge_reply


def test_grounded_conflicting_price_does_not_replace_existing_value():
    quote = Quotation(shipment_id=uuid4(), intake_item_id=uuid4(),
                      base_price=Decimal("100"), currency="USD")
    item = IntakeItem(original_filename="reply.txt", stored_path="reply.txt",
                      content_sha256="a" * 64, raw_text="Price: 120 USD")
    original = FieldEvidence(quotation_id=quote.id, field_name="base_price",
                             value_as_extracted="100", source_filename="original.txt",
                             snippet="100 USD", validation_status=ValidationStatus.accepted)
    extraction = QuoteExtraction(base_price=Decimal("120"), field_evidence=[
        {"field_name": "base_price", "snippet": "Price: 120 USD"}])
    merged, evidence, gaps = _merge_reply(quote, extraction, item, [original])
    assert merged.base_price == Decimal("100")
    assert merged.version == 2
    assert any(e.id == original.id for e in evidence)
    assert any(e.value_as_extracted == "120" and e.validation_status == ValidationStatus.needs_review
               for e in evidence)
    assert any(g.field_name == "base_price" for g in gaps)


def test_grounded_missing_price_is_filled():
    quote = Quotation(shipment_id=uuid4(), intake_item_id=uuid4(), currency="USD")
    item = IntakeItem(original_filename="reply.txt", stored_path="reply.txt",
                      content_sha256="b" * 64, raw_text="Price: 120 USD")
    extraction = QuoteExtraction(base_price=Decimal("120"), field_evidence=[
        {"field_name": "base_price", "snippet": "Price: 120 USD"}])
    merged, evidence, _ = _merge_reply(quote, extraction, item, [])
    assert merged.base_price == Decimal("120")
    assert evidence[0].validation_status == ValidationStatus.accepted

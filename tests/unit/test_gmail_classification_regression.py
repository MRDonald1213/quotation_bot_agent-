from types import SimpleNamespace

from freight_agent.db.engine import get_session
from freight_agent.db.models import IntakeItemRow, utcnow_iso
from freight_agent.services.extraction import _load_classification


def test_load_classification_returns_none_for_empty_gmail_json(settings):
    row = IntakeItemRow(
        id="gmail-invalid-classification",
        shipment_id=None,
        source_type="gmail_body",
        original_filename="gmail-quote.txt",
        stored_path="/tmp/gmail-quote.txt",
        content_sha256="a" * 64,
        sender="supplier@example.com",
        subject="Quotation",
        received_at=utcnow_iso(),
        raw_text="Freight quotation",
        file_size_bytes=16,
        page_or_sheet_info=None,
        classification_status="quotation",
        classification_reason="Automatic Gmail quotation classification",
        classification_confidence=0.8,
        classification_json="{}",
        processing_status="pending",
        created_at=utcnow_iso(),
    )

    with get_session(settings) as session:
        session.add(row)
        session.commit()
        row_id = row.id

    classification = _load_classification(settings, SimpleNamespace(id=row_id))

    assert classification is None

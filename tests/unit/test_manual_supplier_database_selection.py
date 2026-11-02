import json

from freight_agent.db.engine import get_session
from freight_agent.db.models import IntakeItemRow, SupplierDatabaseRow, utcnow_iso
from freight_agent.services.supplier_workflow import process_supplier_quotation


def test_manual_database_override_skips_routing_and_extracts_schema(settings):
    now = utcnow_iso()
    intake = IntakeItemRow(
        id="manual-db-intake", shipment_id=None, source_type="gmail_body",
        original_filename="gmail-quotation.txt", stored_path="/tmp/gmail-quotation.txt",
        content_sha256="b" * 64, sender="carrier@example.com", subject="Rate",
        received_at=now, raw_text="Carrier Name: Baltic Lines\nEmail: sales@baltic.example\n",
        file_size_bytes=70, page_or_sheet_info=None, classification_status="pending",
        classification_reason=None, classification_confidence=None, classification_json=None,
        processing_status="pending", created_at=now,
    )
    database = SupplierDatabaseRow(
        id="manual-db", name="Carrier Master", description="Freight carriers",
        category="carrier", routing_aliases_json='[]', csv_filename="carriers.csv",
        csv_path=str(settings.workspace_dir / "carriers.csv"),
        schema_json=json.dumps([
            {"name": "carrier_name", "label": "Carrier Name", "type": "text", "required": True, "aliases": ["Carrier"]},
            {"name": "contact_email", "label": "Email", "type": "text", "required": True, "aliases": ["Email address"]},
        ]), record_count=0, status="active", created_at=now, updated_at=now,
    )
    with get_session(settings) as session:
        session.add_all([intake, database])
        session.commit()
        intake_id = intake.id
        database_id = database.id

    action = process_supplier_quotation(settings, intake_id, database_id)

    assert action["database_id"] == database_id
    assert action["database_name"] == "Carrier Master"
    assert action["status"] == "ready_for_approval"
    assert action["proposed_row"] == {
        "carrier_name": "Baltic Lines",
        "contact_email": "sales@baltic.example",
    }

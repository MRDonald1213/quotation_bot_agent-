import json
from pathlib import Path
import pytest
from freight_agent.db.engine import get_session
from freight_agent.db.models import SupplierDatabaseRow, utcnow_iso, QuotationRow
from freight_agent.services.intake import register_intake
from freight_agent.services import destination_proposals as proposals
from freight_agent.services.supplier_workflow import run_quotation_pipeline
from freight_agent.domain.exceptions import ValidationFailureError


def setup(settings, tmp_path, text="Supplier Name: Baltic\nPrice: 1200"):
    file = tmp_path / "quotation.txt"
    file.write_text(text)
    intake, _ = register_intake(settings, None, file)
    for key, fields in [("a", ["Supplier_Name", "Price"]), ("b", ["Supplier_Name"])]:
        csv = tmp_path / (key + ".csv")
        csv.write_text(",".join(fields) + "\n")
        schema = [{"name": name, "label": name.replace("_", " "), "type": "currency" if name == "Price" else "text", "required": True} for name in fields]
        with get_session(settings) as session:
            session.add(SupplierDatabaseRow(id=key, name="Baltic " + key, description="Baltic supplier records", category="carrier",
                routing_aliases_json='[]', csv_filename=csv.name, csv_path=str(csv), schema_json=json.dumps(schema),
                record_count=0, status="active", created_at=utcnow_iso(), updated_at=utcnow_iso()))
            session.commit()
    return str(intake.id)


def test_multiple_destinations_are_extracted_before_any_canonical_quote(settings, tmp_path, monkeypatch):
    intake_id = setup(settings, tmp_path)
    monkeypatch.setattr("freight_agent.services.supplier_workflow._ensure_quotation_extracted", lambda *args: pytest.fail("general extraction must not run"))
    result = run_quotation_pipeline(settings, intake_id)
    assert {p['database_id'] for p in result['proposals']} == {'a', 'b'}
    assert all(p['status'] == 'ready_for_approval' for p in result['proposals'])
    assert result['proposals'][0]['values']['Price'] == '1200'
    assert 'Price' not in result['proposals'][1]['values']
    assert len(proposals.process(settings, intake_id)['proposals']) == 2
    assert len(proposals.list_proposals(settings)) == 2
    with get_session(settings) as session:
        assert session.query(QuotationRow).count() == 0


def test_each_destination_requires_its_own_approval_and_import_is_idempotent(settings, tmp_path):
    intake_id = setup(settings, tmp_path)
    rows = proposals.process(settings, intake_id)['proposals']
    a, b = rows
    with pytest.raises(ValidationFailureError): proposals.decide(settings, a['id'], 'import')
    proposals.decide(settings, a['id'], 'approve')
    assert (tmp_path/'a.csv').read_text() == 'Supplier_Name,Price\n'
    proposals.decide(settings, a['id'], 'import')
    proposals.decide(settings, a['id'], 'import')
    assert len((tmp_path/'a.csv').read_text().splitlines()) == 2
    assert (tmp_path/'b.csv').read_text() == 'Supplier_Name\n'
    assert proposals.decide(settings, b['id'], 'reject')['status'] == 'rejected'


def test_no_match_does_not_call_model(settings, tmp_path, fake_llm):
    source = tmp_path/'other.txt'; source.write_text('Unrelated document')
    intake, _ = register_intake(settings, None, source)
    assert proposals.process(settings, str(intake.id))['proposals'] == []
    assert fake_llm.prompts == []


def test_schema_change_invalidates_approval(settings, tmp_path):
    intake_id = setup(settings, tmp_path)
    row = proposals.process(settings, intake_id)['proposals'][0]
    with get_session(settings) as session:
        db = session.get(SupplierDatabaseRow, row['database_id'])
        db.schema_json = '[]'; session.commit()
    with pytest.raises(ValidationFailureError): proposals.decide(settings, row['id'], 'approve')


def test_failure_in_one_destination_does_not_stop_others(settings, tmp_path, monkeypatch):
    intake_id = setup(settings, tmp_path)
    real = proposals.extract
    def extract(settings, schema, source):
        if len(schema) == 2: raise RuntimeError('failure')
        return real(settings, schema, source)
    monkeypatch.setattr(proposals, 'extract', extract)
    rows = proposals.process(settings, intake_id)['proposals']
    assert [p['status'] for p in rows] == ['failed', 'ready_for_approval']


def test_model_values_require_exact_grounded_evidence(settings, fake_llm):
    fake_llm.responses = {'DESTINATION_SCHEMA': {'fields': [{'field':'Price','value':'999','snippet':'Price is 1200'}]}}
    result = proposals.extract(settings, [{'name':'Price', 'type':'currency', 'required':True}], 'Price is 1200')
    assert result['values']['Price'] is None
    assert result['missing'] == ['Price']
    assert result['issues']


def test_invalid_types_are_not_importable():
    assert proposals.validated({'name':'Price','type':'currency'}, '1200 per shipment') is None
    assert proposals.validated({'name':'Expiry','type':'date'}, '2026-02-30') is None
    assert proposals.validated({'name':'Insurance','type':'boolean'}, 'maybe') is None


def test_schema_reply_is_processed_without_manual_linking(settings, tmp_path):
    from freight_agent.db.models import GmailAccountRow, GmailMessageRow, IntakeItemRow
    from freight_agent.services.gmail_sync import process_inbound_reply
    intake_id = setup(settings, tmp_path)
    with get_session(settings) as session:
        session.add(GmailAccountRow(id='account',email='test@example.test',status='connected',created_at=utcnow_iso(),updated_at=utcnow_iso()))
        session.flush()
        for key, body in [('original','Supplier Name: Baltic\nPrice: 1200'), ('reply','Thanks for reviewing our quotation.')]:
            session.add(GmailMessageRow(id=key,gmail_message_id=key,gmail_thread_id='thread',account_id='account',
                sender='sales@example.test',recipients_json='[]',subject='Re: Baltic quote',body_text=body,
                classification='SUPPLIER_QUOTATION_REPLY',processing_status='received',source_hash=key,created_at=utcnow_iso()))
        session.flush()
        session.get(IntakeItemRow,intake_id).gmail_message_id='original'
        session.commit()
    proposals.process(settings, intake_id)
    assert process_inbound_reply(settings, 'reply') == 'ready_for_approval'
    rows = proposals.list_proposals(settings)
    assert len(rows) == 4
    assert len({r['intake_id'] for r in rows}) == 2


def test_schema_revision_keeps_imported_history_and_creates_new_extraction(settings, tmp_path):
    intake_id = setup(settings, tmp_path)
    first = proposals.process(settings, intake_id)['proposals'][0]
    proposals.decide(settings, first['id'], 'approve')
    proposals.decide(settings, first['id'], 'import')
    with get_session(settings) as session:
        db = session.get(SupplierDatabaseRow, first['database_id'])
        schema = json.loads(db.schema_json); schema[0]['description']='Supplier identity stated in source'
        db.schema_json=json.dumps(schema); session.commit()
    revised = proposals.process(settings, intake_id)['proposals'][0]
    assert revised['id'] != first['id']
    assert revised['status'] == 'ready_for_approval'
    assert len(proposals.list_proposals(settings)) == 3

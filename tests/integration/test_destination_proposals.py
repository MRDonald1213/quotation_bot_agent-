import json
from pathlib import Path
import pytest
from freight_agent.db.engine import get_session
from freight_agent.db.models import SupplierDatabaseRow, utcnow_iso, QuotationRow
from freight_agent.services.intake import register_intake
from freight_agent.services import destination_proposals as proposals
from freight_agent.services.supplier_workflow import run_quotation_pipeline
from freight_agent.domain.exceptions import ValidationFailureError


def classify_as_quote(fake_llm, *, relevant=True):
    fake_llm.responses['You are a document classifier'] = {
        'is_freight_quote': relevant,
        'reason': 'Deterministic test classification',
        'confidence': 0.99,
        'needs_human_review': False,
        'document_type': 'carrier_quote' if relevant else 'unrelated',
    }


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


def test_multiple_destinations_are_extracted_before_any_canonical_quote(settings, tmp_path, monkeypatch, fake_llm):
    intake_id = setup(settings, tmp_path)
    classify_as_quote(fake_llm)
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


def test_each_destination_requires_its_own_approval_and_import_is_idempotent(settings, tmp_path, fake_llm):
    intake_id = setup(settings, tmp_path)
    classify_as_quote(fake_llm)
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


def test_unrelated_document_is_classified_without_running_destination_extraction(settings, tmp_path, fake_llm):
    source = tmp_path/'other.txt'; source.write_text('Unrelated document')
    intake, _ = register_intake(settings, None, source)
    classify_as_quote(fake_llm, relevant=False)
    assert proposals.process(settings, str(intake.id))['proposals'] == []
    assert len(fake_llm.prompts) == 1
    assert 'You are a document classifier' in fake_llm.prompts[0]


def test_schema_change_invalidates_approval(settings, tmp_path, fake_llm):
    intake_id = setup(settings, tmp_path)
    classify_as_quote(fake_llm)
    row = proposals.process(settings, intake_id)['proposals'][0]
    with get_session(settings) as session:
        db = session.get(SupplierDatabaseRow, row['database_id'])
        db.schema_json = '[]'; session.commit()
    with pytest.raises(ValidationFailureError): proposals.decide(settings, row['id'], 'approve')


def test_failure_in_one_destination_does_not_stop_others(settings, tmp_path, monkeypatch, fake_llm):
    intake_id = setup(settings, tmp_path)
    classify_as_quote(fake_llm)
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


def test_schema_reply_is_processed_without_manual_linking(settings, tmp_path, fake_llm):
    from freight_agent.db.models import GmailAccountRow, GmailMessageRow, IntakeItemRow
    from freight_agent.services.gmail_sync import process_inbound_reply
    intake_id = setup(settings, tmp_path)
    classify_as_quote(fake_llm)
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


def test_supplier_reply_creates_detailed_follow_up_and_original_message_becomes_solved(
    settings, tmp_path,
):
    from freight_agent.db.models import (
        DestinationProposalRow, GmailAccountRow, GmailMessageRow,
        IntakeItemRow, SupplierEmailDraftRow,
    )
    from freight_agent.services.gmail_drafts import list_drafts
    from freight_agent.services.supplier_workflow import generate_proposal_clarification_drafts

    intake_id = setup(settings, tmp_path)
    proposal_id = "proposal-for-follow-up"
    schema = [
        {"name": "Supplier_Name", "label": "Supplier name", "type": "text", "required": True,
         "scope": "supplier_master"},
        {"name": "Price", "label": "Quoted price", "type": "currency", "required": True,
         "scope": "quotation"},
    ]
    result = {
        "values": {"Supplier_Name": "Baltic", "Price": "1200 per shipment"},
        "missing": [], "uncertain": ["Price"], "reply_history": [],
        "questions": ["Please confirm the quoted price."],
    }
    now = utcnow_iso()
    with get_session(settings) as session:
        intake = session.get(IntakeItemRow, intake_id)
        session.add(GmailAccountRow(
            id="follow-up-account", email="agent@example.test", status="connected",
            created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(GmailMessageRow(
            id="follow-up-source", gmail_message_id="follow-up-source",
            gmail_thread_id="follow-up-thread", account_id="follow-up-account",
            sender="sales@example.test", recipients_json="[]", subject="Baltic quote",
            body_text="Please see our offer", classification="NEW_QUOTATION",
            processing_status="processed", source_hash="follow-up-source", created_at=now,
        ))
        intake.gmail_message_id = "follow-up-source"
        session.add(DestinationProposalRow(
            id=proposal_id, intake_id=intake_id, database_id="a", status="needs_information",
            schema_json=json.dumps(schema), revision_key="follow-up-revision",
            source_hash="follow-up-source", result_json=json.dumps(result),
            created_at=now, updated_at=now,
        ))
        session.commit()

    first = generate_proposal_clarification_drafts(settings, proposal_id, {
        **result, "schema": schema,
    })
    first_id = first[0]["id"]
    with get_session(settings) as session:
        draft = session.get(SupplierEmailDraftRow, first_id)
        draft.status = "sent"
        draft.sent_at = utcnow_iso()
        reply_result = {
            **result, "missing": ["Price"], "uncertain": [],
            "reply_history": [{"text": "It is our standard price."}],
        }
        proposal = session.get(DestinationProposalRow, proposal_id)
        proposal.result_json = json.dumps(reply_result)
        proposal.updated_at = utcnow_iso()
        session.commit()

    follow_up = generate_proposal_clarification_drafts(settings, proposal_id, {
        **reply_result, "schema": schema,
    })
    assert follow_up
    with get_session(settings) as session:
        follow_up_row = session.get(SupplierEmailDraftRow, follow_up[0]["id"])
        assert follow_up_row.subject.startswith("Follow-up:")
        assert "previous reply did not fully answer" in follow_up_row.body
        assert "Please provide a more detailed response" in follow_up_row.body
        assert json.loads(session.get(SupplierEmailDraftRow, first_id).requested_fields_json) == ["Price"]

    statuses = {draft["id"]: draft["resolution_status"] for draft in list_drafts(settings)}
    assert statuses[first_id] == "follow_up_required"

    resolved_result = {
        **reply_result,
        "values": {"Supplier_Name": "Baltic", "Price": "1200 USD"},
        "missing": [], "uncertain": [],
    }
    with get_session(settings) as session:
        proposal = session.get(DestinationProposalRow, proposal_id)
        proposal.result_json = json.dumps(resolved_result)
        proposal.updated_at = utcnow_iso()
        session.commit()
    generate_proposal_clarification_drafts(settings, proposal_id, {
        **resolved_result, "schema": schema,
    })
    statuses = {draft["id"]: draft["resolution_status"] for draft in list_drafts(settings)}
    assert statuses[first_id] == "solved"


def test_schema_revision_keeps_imported_history_and_creates_new_extraction(settings, tmp_path, fake_llm):
    intake_id = setup(settings, tmp_path)
    classify_as_quote(fake_llm)
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

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from freight_agent.api.app import create_app
from freight_agent.domain.models import Shipment, IntakeItem, Quotation, QuotationDetails, FieldEvidence
from freight_agent.domain.enums import ReviewStatus
from freight_agent.services.common import with_session
from freight_agent.services import workspace


@pytest.fixture()
def quote(settings):
    with with_session(settings) as repos, repos.atomic():
        shipment = Shipment(reference='TEST-LANE', name='Demo lane', origin='Shenzhen', destination='Beijing', cargo_description='Parts', target_currency='USD')
        repos.add_shipment(shipment)
        intake = IntakeItem(original_filename='offer.txt', stored_path='offer.txt', content_sha256='e'*64,
                            raw_text='TruckCO Shenzhen Beijing 120 USD per shipment', shipment_id=shipment.id)
        repos.add_intake(intake)
        carrier = repos.get_or_create_carrier('TruckCO')
        q = Quotation(shipment_id=shipment.id, intake_item_id=intake.id, carrier_id=carrier.id,
                      origin='Shenzhen', destination='Beijing', currency='USD', base_price=Decimal('120'),
                      total_known_cost=Decimal('120'), valid_until=date(2099,1,1), delivery_scope='door_to_door',
                      transit_days_min=2, transit_days_max=3, insurance_included=True, customs_included=True,
                      details=QuotationDetails(pricing_unit='shipment'))
        repos.add_quotation(q)
        required = {"carrier_name":"TruckCO", "origin":"Shenzhen", "destination":"Beijing", "base_price":"120", "currency":"USD", "pricing_unit":"shipment", "valid_until":"2099-01-01"}
        repos.add_evidence([FieldEvidence(quotation_id=q.id, field_name=field, value_as_extracted=value,
            source_filename="offer.txt", snippet=value, validation_status="accepted") for field,value in required.items()])
        return q


def test_login_protects_api_and_rejects_bad_credentials(settings):
    config=settings.model_copy(update={'demo_login_enabled':True})
    with TestClient(create_app(config)) as client:
        assert client.get('/api/workspace/quotes').status_code == 401
        assert client.post('/api/login',json={'username':'admin','password':'wrong'}).status_code == 401
        response=client.post('/api/login',json={'username':'admin','password':'1234'})
        assert response.status_code == 200
        assert 'HttpOnly' in response.headers['set-cookie']
        assert client.get('/api/workspace/quotes').status_code == 200
        client.post('/api/logout')
        assert client.get('/api/workspace/quotes').status_code == 401


def test_schema_roundtrip_and_optional_supplier_id(settings,quote):
    data=workspace.detail(settings,str(quote.id))
    assert data['quotation']['details']['pricing_unit']=='shipment'
    assert next(f for f in data['fields'] if f['field']=='supplier_id')['value'] is None
    assert 'supplier_id' not in data['missing']


@pytest.mark.parametrize('disposition',['rejected','invalid','archived'])
def test_archive_preserves_record_and_reason(settings,quote,disposition):
    workspace.archive(settings,str(quote.id),disposition,'Operator decision')
    assert workspace.list_quotes(settings)==[]
    rows=workspace.list_quotes(settings,True)
    assert rows[0]['disposition']==disposition
    assert rows[0]['disposition_reason']=='Operator decision'
    assert workspace.detail(settings,str(quote.id))['sources']


def test_contextual_multiple_answers_and_no_currency_guess(settings,quote):
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));q.carrier_id=None;q.base_price=None;q.currency=None
        repos.update_quotation(q)
    turn=workspace.start_questions(settings,str(quote.id))
    # Three questions, explicitly including currency.
    result=workspace.answer(settings,str(quote.id),turn['id'],'TruckCO, 120, USD')
    assert result['status']=='updated'
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id))
        assert q.base_price==Decimal('120') and q.currency=='USD'
        assert repos.get_carrier(str(q.carrier_id)).name=='TruckCO'
        assert len(repos.versions_for_quotation(q.id))==2
    assert workspace.answer(settings,str(quote.id),turn['id'],'TruckCO, 120, USD')==result


def test_confirm_missing_value_never_sends_or_changes_quote(settings,quote):
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));q.details.pricing_unit=None;repos.update_quotation(q)
    turn=workspace.start_questions(settings,str(quote.id))
    assert workspace.answer(settings,str(quote.id),turn['id'],'Confirm')['status']=='needs_clarification'
    with with_session(settings) as repos:
        assert repos.get_quotation(str(quote.id)).details.pricing_unit is None


def test_comparison_unknown_units_excluded(settings,quote):
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));q.review_status=ReviewStatus.imported;q.details.pricing_unit=None;repos.update_quotation(q)
    result=workspace.compare(settings,str(quote.shipment_id))
    assert result['selected_ids']==[]
    assert 'Pricing unit is unknown' in result['rows'][0]['exclusions']


def test_send_requires_confirmation_before_any_network(settings,monkeypatch):
    from freight_agent.services.gmail_send import send_approved_draft
    from freight_agent.domain.exceptions import ValidationFailureError
    monkeypatch.setattr('freight_agent.services.gmail_send._read_credentials',lambda *_:pytest.fail('No credentials should be read'))
    with pytest.raises(ValidationFailureError):
        send_approved_draft(settings,'none','Confirm','key')


def test_status_does_not_expose_paths(settings):
    with TestClient(create_app(settings)) as client:
        data=client.get('/api/status').json()
        assert data['database']['path']=='configured'
        assert data['workspace']=='configured'


def test_complete_quotation_approval_import_and_comparison(settings,quote):
    workspace.approve(settings,str(quote.id))
    first=workspace.import_quote(settings,str(quote.id))
    assert first['already_imported'] is False
    assert workspace.import_quote(settings,str(quote.id))['already_imported'] is True
    result=workspace.compare(settings,str(quote.shipment_id),'Which quotation is cheapest?')
    assert result['selected_ids']==[str(quote.id)]
    assert '120' in result['explanation']
    fastest=workspace.compare(settings,str(quote.shipment_id),'Which supplier offers fastest delivery?')
    assert fastest['selected_ids']==[str(quote.id)]
    assert fastest['highlight_fields']==['transit_time']


def test_missing_unit_reply_completes_question_and_invalidates_approval(settings,quote):
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));q.details.pricing_unit=None;q.review_status=ReviewStatus.approved;repos.update_quotation(q)
    turn=workspace.start_questions(settings,str(quote.id))
    workspace.answer(settings,str(quote.id),turn['id'],'per shipment')
    with with_session(settings) as repos:
        current=repos.get_quotation(str(quote.id))
        assert current.details.pricing_unit=='shipment'
        assert current.review_status==ReviewStatus.extracted
    assert 'pricing_unit' not in workspace.detail(settings,str(quote.id))['missing']


def test_incompatible_currencies_need_explicit_fx(settings,quote):
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));q.currency='EUR';q.review_status=ReviewStatus.imported;repos.update_quotation(q)
    result=workspace.compare(settings,str(quote.shipment_id))
    assert result['selected_ids']==[]
    assert 'No explicit configured FX rate' in result['rows'][0]['exclusions']


def test_price_does_not_infer_unit_or_currency():
    from freight_agent.services.validation import validate_extraction
    from freight_agent.domain.models import QuoteExtraction
    extraction=QuoteExtraction(base_price=Decimal('1200'),field_evidence=[{'field_name':'base_price','snippet':'$1,200'}])
    cleaned,*_=validate_extraction(extraction,'$1,200','quote.txt')
    assert cleaned.base_price==Decimal('1200')
    assert cleaned.currency is None and cleaned.pricing_unit is None


def test_unlabeled_supplier_name_with_real_evidence():
    from freight_agent.services.validation import validate_extraction
    from freight_agent.domain.models import QuoteExtraction
    extraction=QuoteExtraction(carrier_name='TruckCO',field_evidence=[{'field_name':'carrier_name','snippet':'TruckCO'}])
    cleaned,_,statuses,_=validate_extraction(extraction,'TruckCO, Shenzhen to Beijing, $1,200','quote.txt')
    assert cleaned.carrier_name=='TruckCO'
    assert str(statuses['carrier_name'])=='accepted'


def test_confirmation_has_context_and_never_sends(settings,quote):
    turn=workspace.request_confirmation(settings,str(quote.id))
    result=workspace.answer(settings,str(quote.id),turn['id'],'Confirm')
    assert result['status']=='confirmed'
    assert workspace.detail(settings,str(quote.id))['quotation']['version']==1


def test_contextual_reply_conflict_can_be_resolved_with_source(settings,quote):
    from freight_agent.db.models import ClarificationTurnRow, utcnow_iso
    import json
    with with_session(settings) as repos, repos.atomic():
        turn=ClarificationTurnRow(id=str(uuid4()),quotation_id=str(quote.id),quotation_version=1,
            questions_json=json.dumps([{'id':'price','field':'base_price','text':'Please confirm the base price.'}]),created_at=utcnow_iso())
        repos.session.add(turn)
        turn_id=turn.id
    workspace.answer(settings,str(quote.id),turn_id,'130')
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id))
        assert q.base_price==Decimal('120')
        conflict=next(e for e in repos.evidence_for_quotation(str(quote.id)) if e.field_name=='base_price' and e.validation_status=='needs_review')
    workspace.resolve(settings,str(quote.id),str(conflict.id),True,'Supplier explicitly corrected the price')
    with with_session(settings) as repos:
        assert repos.get_quotation(str(quote.id)).base_price==Decimal('130')
    assert 'base_price' not in workspace.detail(settings,str(quote.id))['conflicts']


def test_waiver_required_and_audited(settings,quote):
    from freight_agent.domain.exceptions import ValidationFailureError
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));q.details.pricing_unit=None;repos.update_quotation(q)
    with pytest.raises(ValidationFailureError):workspace.waive_field(settings,str(quote.id),'pricing_unit','')
    workspace.waive_field(settings,str(quote.id),'pricing_unit','Operator will review rate basis outside the comparison')
    assert 'pricing_unit' not in workspace.detail(settings,str(quote.id))['missing']
    with with_session(settings) as repos:
        assert any(g.status=='waived' for g in repos.list_gaps(str(quote.shipment_id)))


def test_v8_migration_preserves_quotation_and_evidence(settings,quote):
    from freight_agent.db.engine import get_session
    from freight_agent.db.migrations import init_schema, schema_status
    from sqlalchemy import text
    with get_session(settings) as session:
        for column in ['details_json','disposition','disposition_reason','archived_at']:
            session.execute(text(f'ALTER TABLE quotations DROP COLUMN {column}'))
        for column in ['quotation_id','clarification_turn_id']:
            session.execute(text(f'ALTER TABLE supplier_email_drafts DROP COLUMN {column}'))
        session.execute(text('DELETE FROM schema_version WHERE version>=9'))
        session.commit()
        assert init_schema(session)==['v9', 'v10']
        assert schema_status(session)==(True,10)
        assert init_schema(session)==[]
    with with_session(settings) as repos:
        restored=repos.get_quotation(str(quote.id))
        assert restored.base_price==Decimal('120')
        assert len(repos.evidence_for_quotation(str(quote.id)))==7
        assert restored.details.pricing_unit is None


def test_human_send_idempotency_and_approval_invalidation(settings,monkeypatch):
    from freight_agent.db.models import GmailAccountRow, utcnow_iso
    from freight_agent.db.engine import get_session
    from freight_agent.services.gmail_drafts import create_draft,approve_draft,save_draft
    from freight_agent.services.gmail_send import send_approved_draft
    from freight_agent.domain.exceptions import ValidationFailureError
    from unittest.mock import MagicMock
    now=utcnow_iso()
    with get_session(settings) as session:
        session.add(GmailAccountRow(id='test-account',email='demo@example.test',status='connected',created_at=now,updated_at=now));session.commit()
    # Preparing/editing a draft must never contact Gmail.
    monkeypatch.setattr('freight_agent.services.gmail_drafts._credentials',lambda *_:pytest.fail('Draft preparation contacted Gmail'))
    draft=create_draft(settings,recipient='supplier@example.test',cc='',subject='Clarification',body='Please confirm the price.',gmail_account_id='test-account')
    with pytest.raises(ValidationFailureError):send_approved_draft(settings,draft['id'],'SEND',draft['id'])
    approve_draft(settings,draft['id'])
    save_draft(settings,draft['id'],recipient='supplier@example.test',cc='',subject='Updated clarification',body='Please confirm currency.')
    with pytest.raises(ValidationFailureError):send_approved_draft(settings,draft['id'],'SEND',draft['id'])
    approve_draft(settings,draft['id'])
    credentials=MagicMock();credentials.expired=False
    service=MagicMock();service.users().drafts().create().execute.return_value={'id':'remote-draft'}
    service.users().drafts().send().execute.return_value={'id':'remote-message'}
    monkeypatch.setattr('freight_agent.services.gmail_send._read_credentials',lambda *_:credentials)
    monkeypatch.setattr('freight_agent.services.gmail_send.build',lambda *_args,**_kwargs:service)
    assert send_approved_draft(settings,draft['id'],'SEND',draft['id'])['status']=='sent'
    assert send_approved_draft(settings,draft['id'],'SEND',draft['id'])['idempotent'] is True
    assert service.users().drafts().send().execute.call_count==1


def test_same_lane_different_units_not_ranked(settings,quote):
    with with_session(settings) as repos, repos.atomic():
        q=repos.get_quotation(str(quote.id));q.review_status=ReviewStatus.imported;repos.update_quotation(q)
        other=q.model_copy(deep=True);other.id=uuid4();other.details.pricing_unit='kg';repos.add_quotation(other)
        repos.add_evidence([e.model_copy(update={'id':uuid4(),'quotation_id':other.id}) for e in repos.evidence_for_quotation(str(q.id))])
    result=workspace.compare(settings,str(quote.shipment_id))
    assert result['selected_ids']==[]
    assert all('Select one pricing unit before comparing' in row['exclusions'] for row in result['rows'])
    assert workspace.compare(settings,str(quote.shipment_id),pricing_unit='shipment')['selected_ids']==[str(quote.id)]


def test_informal_answer_uses_question_context_and_grounding(settings,quote,fake_llm):
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));q.carrier_id=None;q.base_price=None;repos.update_quotation(q)
    turn=workspace.start_questions(settings,str(quote.id))
    fake_llm.responses={'filename="reply.txt"':{'carrier_name':'TruckCO','base_price':'120','field_evidence':[
        {'field_name':'carrier_name','snippet':'TruckCO'},{'field_name':'base_price','snippet':'120'}]}}
    result=workspace.answer(settings,str(quote.id),turn['id'],"It is TruckCO and the freight price is 120.")
    assert result['status']=='updated'
    assert any('question_context' in prompt and 'carrier_name' in prompt for prompt in fake_llm.prompts)
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));assert q.currency=='USD' and q.base_price==Decimal('120')


def test_malformed_contextual_reply_does_not_mutate_quote(settings,quote,fake_llm):
    from freight_agent.domain.exceptions import ModelUnavailableError
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));q.details.pricing_unit=None;repos.update_quotation(q)
    turn=workspace.start_questions(settings,str(quote.id))
    fake_llm.responses={'filename="reply.txt"':{'base_price':'not a number'}}
    with pytest.raises(ModelUnavailableError):workspace.answer(settings,str(quote.id),turn['id'],'One for each parcel, maybe two')
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));assert q.version==1 and q.base_price==Decimal('120')


def test_uncertain_delivery_is_locked_until_human_reconciliation(settings,monkeypatch):
    from freight_agent.db.models import GmailAccountRow, utcnow_iso
    from freight_agent.db.engine import get_session
    from freight_agent.services.gmail_drafts import create_draft,approve_draft
    from freight_agent.services.gmail_send import send_approved_draft,reconcile_delivery
    from freight_agent.domain.exceptions import ConfigurationError,ValidationFailureError
    now=utcnow_iso()
    with get_session(settings) as session:
        session.add(GmailAccountRow(id='uncertain-account',email='uncertain@example.test',status='connected',created_at=now,updated_at=now));session.commit()
    draft=create_draft(settings,recipient='supplier@example.test',cc='',subject='Clarification',body='Please confirm price.',gmail_account_id='uncertain-account')
    approve_draft(settings,draft['id'])
    monkeypatch.setattr('freight_agent.services.gmail_send._read_credentials',lambda *_:(_ for _ in ()).throw(OSError('offline')))
    with pytest.raises(ConfigurationError):send_approved_draft(settings,draft['id'],'SEND',draft['id'])
    with pytest.raises(ValidationFailureError):send_approved_draft(settings,draft['id'],'SEND',draft['id'])
    assert reconcile_delivery(settings,draft['id'],False,'','Checked Gmail Sent and confirmed no delivery')['status']=='draft'


def test_archive_is_read_only(settings,quote):
    from freight_agent.domain.exceptions import ValidationFailureError
    workspace.archive(settings,str(quote.id),'archived','Closed shipment')
    with pytest.raises(ValidationFailureError):workspace.start_questions(settings,str(quote.id))


def test_unsafe_origin_hostname_is_refused(settings):
    with TestClient(create_app(settings)) as client:
        assert client.post('/api/login',headers={'Origin':'http://localhost.evil.test'},json={'username':'admin','password':'1234'}).status_code==403


def test_clear_data_handles_new_records_and_preserves_connected_csv(settings,quote):
    import json
    from freight_agent.db.models import SupplierDatabaseRow,utcnow_iso
    from freight_agent.services.review import clear_quotation_data
    settings.workspace_dir.mkdir(parents=True,exist_ok=True)
    directory=settings.workspace_dir/'supplier_databases';directory.mkdir()
    csv=directory/'suppliers.csv';csv.write_text('Supplier_Name,Unit\nTruckCO,shipment\n')
    with with_session(settings) as repos, repos.atomic():
        repos.session.add(SupplierDatabaseRow(id='preserved-db',name='Supplier records',csv_filename='suppliers.csv',csv_path=str(csv),
            schema_json='[]',routing_aliases_json='[]',record_count=1,status='active',created_at=utcnow_iso(),updated_at=utcnow_iso()))
    workspace.request_confirmation(settings,str(quote.id))
    result=clear_quotation_data(settings)
    assert result['quotations']==1
    assert csv.is_file()
    with with_session(settings) as repos:assert repos.list_quotations()==[]


def test_unlabeled_price_email_reaches_semantic_classification():
    from freight_agent.services.gmail_sync import _classify_supplier_message
    assert _classify_supplier_message('Rates','TruckCO, Shenzhen to Beijing, $1,200.',[])[0]=='UNCERTAIN'


def test_draft_is_saved_locally_without_gmail(settings,quote):
    from freight_agent.domain.exceptions import ValidationFailureError
    with with_session(settings) as repos:
        q=repos.get_quotation(str(quote.id));q.details.pricing_unit=None;repos.update_quotation(q)
    turn=workspace.start_questions(settings,str(quote.id))
    workspace.save_local_draft(settings,str(quote.id),turn['id'],'','Clarification','Please confirm the pricing unit.')
    with with_session(settings) as repos:
        draft=repos.get_draft(turn['id']);assert draft.body=='Please confirm the pricing unit.' and draft.recipient is None
    assert workspace.list_quotes(settings)[0]['stage']=='Clarification draft'
    with pytest.raises(ValidationFailureError):workspace.prepare_draft(settings,str(quote.id),turn['id'],'supplier@example.test','Clarification','Revised local text')
    with with_session(settings) as repos:assert repos.get_draft(turn['id']).body=='Revised local text'
    workspace.answer(settings,str(quote.id),turn['id'],'shipment')
    with with_session(settings) as repos:assert str(repos.get_draft(turn['id']).status)=='cancelled'


def test_pipeline_without_database_does_not_extract(settings,tmp_path,fake_llm):
    from freight_agent.services.intake import register_intake
    from freight_agent.services.supplier_workflow import run_quotation_pipeline
    path=tmp_path/'uncertain.txt';path.write_text('TruckCO, Shenzhen to Beijing, $1200')
    item,_=register_intake(settings,None,path)
    fake_llm.responses={'filename="uncertain.txt"':{'is_freight_quote':True,'reason':'uncertain offer','confidence':0.4,'needs_human_review':True,'document_type':'ambiguous'}}
    result=run_quotation_pipeline(settings,str(item.id))
    assert result['action']['id'] is None
    with with_session(settings) as repos:
        assert repos.list_quotations()==[]
        assert str(repos.get_intake(str(item.id)).classification_status)=='pending'
    assert result['proposals']==[]
    assert fake_llm.prompts==[]


def test_parser_location_overrides_model_claim():
    from freight_agent.services.validation import validate_extraction
    from freight_agent.domain.models import QuoteExtraction
    extraction=QuoteExtraction(carrier_name='TruckCO',field_evidence=[{'field_name':'carrier_name','snippet':'TruckCO','page_or_sheet':'page 99'}])
    _,evidence,_,_=validate_extraction(extraction,'[page 1]\nTruckCO\n[page 2]\nOther text','quote.pdf')
    assert evidence[0].page_or_sheet=='page 1'


def test_sent_question_order_and_stale_send_guard(settings,quote,monkeypatch):
    from freight_agent.db.models import GmailAccountRow,ClarificationTurnRow,utcnow_iso
    from freight_agent.services.gmail_send import send_approved_draft
    from freight_agent.domain.exceptions import ValidationFailureError
    import json
    with with_session(settings) as repos, repos.atomic():
        q=repos.get_quotation(str(quote.id));q.details.pricing_unit=None;q.valid_until=None;repos.update_quotation(q)
        repos.session.add(GmailAccountRow(id='context-account',email='context@example.test',status='connected',created_at=utcnow_iso(),updated_at=utcnow_iso()))
    turn=workspace.start_questions(settings,str(quote.id))
    reordered=list(reversed(turn['questions']))
    body='Please clarify:\n'+'\n'.join(q['text'] for q in reordered)
    draft=workspace.prepare_draft(settings,str(quote.id),turn['id'],'supplier@example.test','Clarification',body)
    with with_session(settings) as repos:
        saved=json.loads(repos.session.get(ClarificationTurnRow,turn['id']).questions_json)
        assert [q['field'] for q in saved]==[q['field'] for q in reordered]
    values={'valid_until':'2099-01-01','pricing_unit':'shipment'}
    workspace.answer(settings,str(quote.id),turn['id'],', '.join(values[q['field']] for q in reordered))
    monkeypatch.setattr('freight_agent.services.gmail_send._read_credentials',lambda *_:pytest.fail('Stale send reached transport'))
    with pytest.raises(ValidationFailureError):send_approved_draft(settings,draft['id'],'SEND',draft['id'])


def test_source_download_is_available_in_archive(settings,quote):
    workspace.archive(settings,str(quote.id),'archived','Closed')
    with TestClient(create_app(settings)) as client:
        response=client.get(f'/api/workspace/quotes/{quote.id}/sources/{quote.intake_item_id}/download')
        assert response.status_code==200 and 'TruckCO' in response.text
        assert client.get(f'/api/workspace/quotes/{quote.id}/sources/{uuid4()}/download').status_code==404


def test_received_documents_share_quotation_queue_without_invented_values(settings,tmp_path):
    from freight_agent.services.intake import register_intake
    source=tmp_path/'received.txt';source.write_text('A newly received quotation awaiting extraction')
    item,_=register_intake(settings,None,source)
    assert workspace.list_quotes(settings)==[]
    rows=workspace.list_quotes(settings,include_intake=True)
    assert len(rows)==1 and rows[0]['record_type']=='intake'
    assert rows[0]['stage']=='Received' and rows[0]['base_price'] is None
    assert rows[0]['filled'] is None and rows[0]['required'] is None
    assert rows[0]['id']==str(item.id)

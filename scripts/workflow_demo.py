"""Synthetic demo only: never reads production data or connects Gmail."""
from pathlib import Path
from datetime import date
from decimal import Decimal
import sys

from freight_agent.config import Settings
from freight_agent.db.engine import get_session
from freight_agent.db.migrations import init_schema
from freight_agent.domain.models import Shipment, IntakeItem, Quotation, QuotationDetails, FieldEvidence
from freight_agent.services.common import with_session

DIRECTORY=Path(__file__).resolve().parents[2]/'video'/'demo-data'/'workflow-v9'
DIRECTORY.mkdir(parents=True,exist_ok=True)
settings=Settings(_env_file=None,database_url=f"sqlite:///{DIRECTORY/'demo.db'}",workspace_dir=DIRECTORY/'workspace',
                  ollama_model='demo-offline',gmail_background_enabled=False,
                  google_oauth_credentials_path=str(DIRECTORY/'no-credentials.json'),google_oauth_token_path=str(DIRECTORY/'no-token.json'))
if '--reset' in sys.argv:
    for suffix in ('', '-wal', '-shm'):
        path = DIRECTORY / ('demo.db' + suffix)
        if path.exists():
            path.unlink()
with get_session(settings) as session:init_schema(session)
with with_session(settings) as repos, repos.atomic():
    if not repos.list_quotations():
        shipment=Shipment(reference='DEMO-SZ-BJ',name='Shenzhen → Beijing',origin='Shenzhen',destination='Beijing',cargo_description='Machine parts',target_currency='USD')
        repos.add_shipment(shipment)
        for index,(name,amount,days,status) in enumerate([('Harbor Freight','1200',5,'imported'),('Northline Cargo','1350',3,'imported'),('Coastal Transit','1280',4,'extracted'),('Summit Logistics','1400',6,'extracted')]):
            raw=f'{name}\nShenzhen to Beijing\nPrice: {amount} USD per shipment\nTransit: {days} days\nValid until 2099-12-31\nDoor to door\nInsurance included\nCustoms included'
            path=settings.workspace_dir/f'offer-{index+1}.txt';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(raw)
            item=IntakeItem(original_filename=path.name,stored_path=str(path),content_sha256=str(index)*64,raw_text=raw,shipment_id=shipment.id,classification_status='relevant',processing_status='processed')
            repos.add_intake(item);carrier=repos.get_or_create_carrier(name)
            q=Quotation(shipment_id=shipment.id,intake_item_id=item.id,carrier_id=carrier.id,quote_reference=f'DEMO-{index+1:03}',origin='Shenzhen',destination='Beijing',currency='USD',base_price=Decimal(amount),total_known_cost=Decimal(amount),valid_until=date(2099,12,31),delivery_scope='door_to_door',transit_days_min=days,transit_days_max=days,insurance_included=True,customs_included=True,details=QuotationDetails(pricing_unit='shipment'),review_status=status,comparison_status='comparable')
            if index==2:q.details.pricing_unit=None;q.valid_until=None;q.comparison_status='incomplete'
            if index==3:q.disposition='rejected';q.disposition_reason='Supplier withdrew the offer'
            repos.add_quotation(q)
            values={'carrier_name':name,'origin':'Shenzhen','destination':'Beijing','currency':'USD','base_price':amount,'pricing_unit':'shipment','valid_until':'2099-12-31'}
            repos.add_evidence([FieldEvidence(quotation_id=q.id,field_name=f,value_as_extracted=v,source_filename=item.original_filename,snippet=v,validation_status='accepted') for f,v in values.items() if not(index==2 and f in {'pricing_unit','valid_until'})])
if __name__=='__main__':
    import uvicorn
    from freight_agent.api.app import create_app
    uvicorn.run(create_app(settings),host='127.0.0.1',port=8767)

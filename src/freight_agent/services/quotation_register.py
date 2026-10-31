from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from sqlalchemy import select

from ..config import Settings
from ..db.engine import get_session
from ..db.models import AuditEventRow, QuotationRegisterRow, SupplierQuotationActionRow, utcnow_iso
from ..domain.exceptions import NotFoundError, ValidationFailureError


def list_register(settings: Settings) -> list[dict]:
    with get_session(settings) as session:
        rows = session.scalars(select(QuotationRegisterRow).order_by(QuotationRegisterRow.created_at.desc())).all()
    return [{"id": row.id, "supplier_master_id": row.supplier_master_id, "database_id": row.database_id,
             "intake_id": row.intake_id, "gmail_message_id": row.gmail_message_id,
             "gmail_thread_id": row.gmail_thread_id, "revision": row.revision, "status": row.status,
             "data": json.loads(row.data_json), "evidence": json.loads(row.evidence_json),
             "source_hash": row.source_hash, "approved_at": row.approved_at,
             "imported_at": row.imported_at, "created_at": row.created_at, "updated_at": row.updated_at}
            for row in rows]


def import_quotation_action(settings: Settings, action_id: str) -> dict:
    now = utcnow_iso()
    with get_session(settings) as session:
        action = session.get(SupplierQuotationActionRow, action_id)
        if action is None:
            raise NotFoundError("Supplier quotation action not found")
        if action.status != "ready_for_approval":
            raise ValidationFailureError("Quotation must have ready_for_approval status before final import")
        proposed = json.loads(action.proposed_row_json)
        evidence = json.loads(action.evidence_json)
        source_hash = hashlib.sha256(json.dumps(proposed, sort_keys=True).encode("utf-8")).hexdigest()
        existing = session.scalars(select(QuotationRegisterRow).where(
            QuotationRegisterRow.intake_id == action.intake_id,
        ).order_by(QuotationRegisterRow.revision.desc())).first()
        revision = 1 if existing is None else existing.revision + 1
        row = QuotationRegisterRow(id=str(uuid4()), database_id=action.database_id,
                                   intake_id=action.intake_id, revision=revision,
                                   status="imported", data_json=json.dumps(proposed),
                                   evidence_json=json.dumps(evidence), source_hash=source_hash,
                                   approved_at=now, imported_at=now, created_at=now, updated_at=now)
        session.add(row)
        action.status = "record_created"
        action.updated_at = now
        action.created_csv_row_json = json.dumps(proposed)
        session.add(AuditEventRow(id=str(uuid4()), entity_type="quotation_register", entity_id=row.id,
                                  action="human_import_approved", details_json=json.dumps({"revision": revision, "action_id": action_id}),
                                  created_at=now))
        session.commit()
    return {"id": row.id, "revision": row.revision, "status": row.status, "imported_at": row.imported_at}

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from ..config import Settings
from ..db.engine import get_session
from ..db.models import AuditEventRow, SupplierDatabaseRow, SupplierMasterRow, utcnow_iso
from ..domain.exceptions import NotFoundError
from .supplier_databases import _parse_csv


def _identity(headers: list[str], row: dict[str, str]) -> str:
    supplier_id = str(row.get("Supplier_ID") or row.get("supplier_id") or "").strip()
    quotation_id = str(row.get("Quotation_ID") or row.get("quotation_id") or "").strip()
    if supplier_id and quotation_id:
        return f"{supplier_id}:{quotation_id}"
    priority = [h for h in headers if "supplier_id" in h.lower() or "supplier id" in h.lower()]
    priority += [h for h in headers if re.search(r"supplier|vendor|carrier|company", h.lower()) and h not in priority]
    priority += [h for h in headers if "email" in h.lower() and h not in priority]
    for header in priority:
        value = str(row.get(header) or "").strip()
        if value:
            return value
    return ""


def sync_supplier_master(settings: Settings, database_id: str) -> dict:
    now = utcnow_iso()
    with get_session(settings) as session:
        database = session.get(SupplierDatabaseRow, database_id)
        if database is None or database.status == "disconnected":
            raise NotFoundError("supplier database not found")
        headers, rows, malformed = _parse_csv(Path(database.csv_path))
        source_hash = hashlib.sha256(Path(database.csv_path).read_bytes()).hexdigest()
        created = 0
        unchanged = 0
        for source_row, row in enumerate(rows, start=2):
            identity = _identity(headers, row)
            existing = session.scalars(select(SupplierMasterRow).where(
                SupplierMasterRow.database_id == database_id,
                SupplierMasterRow.supplier_identity == identity,
            )).first()
            if existing:
                unchanged += 1
                continue
            session.add(SupplierMasterRow(id=str(uuid4()), database_id=database_id,
                                          source_row=source_row, supplier_identity=identity,
                                          record_json=json.dumps(row), source_sha256=source_hash,
                                          created_at=now, updated_at=now))
            created += 1
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_database", entity_id=database_id,
                                  action="supplier_master_synced",
                                  details_json=json.dumps({"created": created, "unchanged": unchanged, "malformed": len(malformed)}),
                                  created_at=now))
        session.commit()
    return {"created": created, "unchanged": unchanged, "malformed_count": len(malformed), "malformed": malformed}


def list_supplier_master(settings: Settings, database_id: str | None = None) -> list[dict]:
    with get_session(settings) as session:
        query = select(SupplierMasterRow)
        if database_id:
            query = query.where(SupplierMasterRow.database_id == database_id)
        rows = session.scalars(query.order_by(SupplierMasterRow.supplier_identity)).all()
    return [{"id": row.id, "database_id": row.database_id, "source_row": row.source_row,
             "supplier_identity": row.supplier_identity, "record": json.loads(row.record_json),
             "source_sha256": row.source_sha256, "created_at": row.created_at, "updated_at": row.updated_at}
            for row in rows]

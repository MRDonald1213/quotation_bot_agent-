from __future__ import annotations

import csv
import json
import re
import shutil
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from ..config import Settings
from ..db.engine import get_session
from ..db.models import AuditEventRow, SupplierDatabaseRow, utcnow_iso
from ..domain.exceptions import NotFoundError, ValidationFailureError


def _parse_csv(path: Path) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
    encodings = ("utf-8-sig", "utf-8", "cp1252")
    for encoding in encodings:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    raise ValidationFailureError("CSV file has no header row")
                headers = [str(name).strip() for name in reader.fieldnames]
                if len(headers) != len(set(headers)):
                    raise ValidationFailureError("CSV header contains duplicate column names")
                if any(not name for name in headers):
                    raise ValidationFailureError("CSV header contains an empty column name")

                identity_headers = [
                    header for header in headers
                    if re.search(r"(^|[_ -])(supplier|vendor|carrier|company|email)([_ -]|$)", header.lower())
                    or header.lower() in {"supplier_id", "supplier name", "supplier_name"}
                ]
                valid_rows: list[dict[str, str]] = []
                malformed_rows: list[dict[str, str]] = []
                for record_number, raw_row in enumerate(reader, start=1):
                    row = {header: str(raw_row.get(header) or "").strip() for header in headers}
                    populated = sum(bool(value) for value in row.values())
                    identity_value = next((row[header] for header in identity_headers if row.get(header)), "")
                    has_identity = bool(identity_value)
                    if populated == 0:
                        continue
                    if not has_identity or populated < 2:
                        malformed_rows.append({"record_number": str(record_number), "reason": "Incomplete supplier record: no usable supplier identity or insufficient attributes", "row": json.dumps(row, ensure_ascii=False)})
                        continue
                    valid_rows.append(row)
                return headers, valid_rows, malformed_rows
        except UnicodeDecodeError:
            continue
    raise ValidationFailureError("CSV encoding must be UTF-8, UTF-8 BOM, or Windows-1252")


def _infer_type(name: str, values: list[str]) -> str:
    lowered = name.lower()
    if "supplier" in lowered or "vendor" in lowered or "carrier" in lowered:
        return "supplier"
    if "currency" in lowered:
        return "text"
    if "date" in lowered or "expires" in lowered or "valid" in lowered:
        return "date"
    if "email" in lowered:
        return "text"
    if "unit" in lowered and "unit_price" not in lowered and "price_unit" not in lowered:
        return "unit"
    if "boolean" in lowered or lowered.startswith("is_"):
        return "boolean"
    cleaned = [value.strip() for value in values if value and value.strip()]
    if not cleaned:
        return "text"
    if all(re.fullmatch(r"(?i)(yes|no|true|false|y|n)", value.strip()) for value in cleaned):
        return "boolean"
    numeric = 0
    for value in cleaned:
        candidate = value.replace(",", "").replace(" ", "")
        candidate = re.sub(r"^[€£$¥]", "", candidate)
        try:
            Decimal(candidate)
            numeric += 1
        except InvalidOperation:
            pass
    if numeric == len(cleaned):
        return "currency" if any(c in v for v in cleaned for c in "€£$¥") or "price" in lowered or "rate" in lowered or "cost" in lowered else "number"
    for value in cleaned:
        try:
            date.fromisoformat(value)
            return "date"
        except ValueError:
            pass
    return "text"


def resolve_supplier_csv_path(settings: Settings, supplied_path: str) -> Path:
    raw = supplied_path.strip()
    if not raw:
        raise ValidationFailureError("enter the full path to a CSV supplier database")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        project_candidate = candidate.resolve()
        workspace_candidate = settings.workspace_dir.resolve() / candidate
        candidate = project_candidate if project_candidate.exists() else workspace_candidate
    candidate = candidate.resolve()
    if not candidate.exists() or not candidate.is_file():
        raise ValidationFailureError(f"CSV supplier database not found: {candidate}")
    if candidate.suffix.lower() != ".csv":
        raise ValidationFailureError("only CSV supplier databases are supported")
    if not os.access(candidate, os.R_OK | os.W_OK):
        raise ValidationFailureError("the CSV file must be readable and writable by the application")
    return candidate


def preview_supplier_database_path(settings: Settings, supplied_path: str) -> dict:
    path = resolve_supplier_csv_path(settings, supplied_path)
    if path.stat().st_size > settings.max_file_size_mb * 1024 * 1024:
        raise ValidationFailureError(f"CSV is larger than the configured limit of {settings.max_file_size_mb} MB")
    fields, record_count = infer_schema(path)
    headers, rows, malformed = _parse_csv(path)
    return {
        "csv_filename": path.name,
        "csv_source_path": str(path),
        "record_count": record_count,
        "malformed_record_count": len(malformed),
        "malformed_records": malformed,
        "schema": fields,
    }


def infer_schema(path: Path) -> tuple[list[dict], int]:
    headers, rows, malformed_rows = _parse_csv(path)
    fields: list[dict] = []
    for header in headers:
        values = [str(row.get(header) or "") for row in rows[:100]]
        fields.append({
            "name": header,
            "label": header.replace("_", " ").replace("-", " ").title(),
            "type": _infer_type(header, values),
            "required": False,
            "description": "",
            "aliases": [],
            "searchable": True,
        })
    return fields, len(rows)


def row_payload(row: SupplierDatabaseRow) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "category": row.category,
        "routing_aliases": json.loads(row.routing_aliases_json),
        "csv_filename": row.csv_filename,
        "csv_path": row.csv_path,
        "schema": json.loads(row.schema_json),
        "record_count": row.record_count,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_supplier_databases(settings: Settings) -> list[dict]:
    with get_session(settings) as session:
        rows = session.scalars(select(SupplierDatabaseRow).order_by(SupplierDatabaseRow.created_at.desc())).all()
        return [row_payload(row) for row in rows if row.status != "disconnected"]


def get_supplier_database(settings: Settings, database_id: str) -> dict:
    with get_session(settings) as session:
        row = session.get(SupplierDatabaseRow, database_id)
        if row is None or row.status == "disconnected":
            raise NotFoundError("supplier database not found")
        return row_payload(row)


def connect_supplier_database(settings: Settings, uploaded_name: str, upload_bytes: bytes,
                               name: str, description: str = "", category: str | None = None,
                               routing_aliases: list[str] | None = None,
                               schema: list[dict] | None = None) -> dict:
    if not uploaded_name.lower().endswith(".csv"):
        raise ValidationFailureError("only CSV supplier databases are supported")
    if len(upload_bytes) > settings.max_file_size_mb * 1024 * 1024:
        raise ValidationFailureError(f"CSV is larger than the configured limit of {settings.max_file_size_mb} MB")

    database_id = str(uuid4())
    directory = settings.workspace_dir.resolve() / "supplier_databases" / database_id
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = Path(uploaded_name).name or "supplier-database.csv"
    path = directory / safe_name
    path.write_bytes(upload_bytes)

    try:
        inferred, record_count = infer_schema(path)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise

    if schema:
        by_name = {field["name"]: field for field in schema if isinstance(field, dict) and field.get("name")}
        final_schema = []
        for inferred_field in inferred:
            custom = by_name.get(inferred_field["name"], {})
            final_schema.append({
                **inferred_field,
                "label": str(custom.get("label") or inferred_field["label"]),
                "type": str(custom.get("type") or inferred_field["type"]),
                "required": bool(custom.get("required", False)),
                "description": str(custom.get("description") or ""),
                "aliases": list(custom.get("aliases") or []),
                "searchable": bool(custom.get("searchable", True)),
            })
    else:
        final_schema = inferred

    now = utcnow_iso()
    with get_session(settings) as session:
        with session.begin():
            row = SupplierDatabaseRow(
                id=database_id,
                name=name.strip() or Path(safe_name).stem,
                description=description.strip(),
                category=(category.strip() if category else None),
                routing_aliases_json=json.dumps(routing_aliases or []),
                csv_filename=safe_name,
                csv_path=str(path),
                schema_json=json.dumps(final_schema),
                record_count=record_count,
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.add(AuditEventRow(
                id=str(uuid4()), entity_type="supplier_database", entity_id=database_id,
                action="connected", details_json=json.dumps({"csv_filename": safe_name, "record_count": record_count}),
                created_at=now,
            ))
            session.flush()
            database_id_value = row.id
            payload = row_payload(row)
        from .supplier_master import sync_supplier_master
        sync_supplier_master(settings, database_id_value)
        return payload


def connect_supplier_database_path(settings: Settings, supplied_path: str, *, name: str,
                                   description: str = "", category: str | None = None,
                                   routing_aliases: list[str] | None = None,
                                   schema: list[dict] | None = None) -> dict:
    path = resolve_supplier_csv_path(settings, supplied_path)
    if path.stat().st_size > settings.max_file_size_mb * 1024 * 1024:
        raise ValidationFailureError(f"CSV is larger than the configured limit of {settings.max_file_size_mb} MB")

    inferred, record_count = infer_schema(path)
    if schema:
        by_name = {field["name"]: field for field in schema if isinstance(field, dict) and field.get("name")}
        final_schema = []
        for inferred_field in inferred:
            custom = by_name.get(inferred_field["name"], {})
            final_schema.append({
                **inferred_field,
                "label": str(custom.get("label") or inferred_field["label"]),
                "type": str(custom.get("type") or inferred_field["type"]),
                "required": bool(custom.get("required", False)),
                "description": str(custom.get("description") or ""),
                "aliases": list(custom.get("aliases") or []),
                "searchable": bool(custom.get("searchable", True)),
            })
    else:
        final_schema = inferred

    database_id = str(uuid4())
    now = utcnow_iso()
    with get_session(settings) as session:
        row = SupplierDatabaseRow(
            id=database_id,
            name=name.strip() or path.stem,
            description=description.strip(),
            category=(category.strip() if category else None),
            routing_aliases_json=json.dumps(routing_aliases or []),
            csv_filename=path.name,
            csv_path=str(path),
            schema_json=json.dumps(final_schema),
            record_count=record_count,
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.add(AuditEventRow(
            id=str(uuid4()), entity_type="supplier_database", entity_id=database_id,
            action="connected", details_json=json.dumps({"connection_type": "csv_path", "csv_path": str(path), "record_count": record_count}),
            created_at=now,
        ))
        session.commit()
        payload = row_payload(row)
    from .supplier_master import sync_supplier_master
    sync_supplier_master(settings, database_id)
    return payload


def update_supplier_database(settings: Settings, database_id: str, updates: dict) -> dict:
    with get_session(settings) as session:
        with session.begin():
            row = session.get(SupplierDatabaseRow, database_id)
            if row is None or row.status == "disconnected":
                raise NotFoundError("supplier database not found")
            allowed = ("name", "description", "category", "routing_aliases", "schema")
            for key in allowed:
                if key not in updates:
                    continue
                if key == "routing_aliases":
                    row.routing_aliases_json = json.dumps(updates[key] or [])
                elif key == "schema":
                    row.schema_json = json.dumps(updates[key])
                else:
                    setattr(row, key, str(updates[key]).strip())
            row.updated_at = utcnow_iso()
            session.add(AuditEventRow(
                id=str(uuid4()), entity_type="supplier_database", entity_id=database_id,
                action="updated", details_json=json.dumps({key: updates[key] for key in updates if key in allowed}),
                created_at=row.updated_at,
            ))
            session.flush()
            return row_payload(row)


def disconnect_supplier_database(settings: Settings, database_id: str) -> dict:
    with get_session(settings) as session:
        with session.begin():
            row = session.get(SupplierDatabaseRow, database_id)
            if row is None:
                raise NotFoundError("supplier database not found")
            row.status = "disconnected"
            row.updated_at = utcnow_iso()
            session.add(AuditEventRow(
                id=str(uuid4()), entity_type="supplier_database", entity_id=database_id,
                action="disconnected", details_json="{}", created_at=row.updated_at,
            ))
            session.flush()
            return row_payload(row)

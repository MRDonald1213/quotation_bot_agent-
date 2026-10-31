from __future__ import annotations

import csv
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import select

from ..config import Settings
from ..db.engine import get_session
from ..db.models import AuditEventRow, SupplierChatRow, SupplierDatabaseRow, SupplierQuotationActionRow, utcnow_iso
from ..domain.exceptions import NotFoundError, ValidationFailureError


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    from .supplier_databases import _parse_csv

    headers, rows, _malformed = _parse_csv(path)
    return headers, rows


def _database_text(database: SupplierDatabaseRow) -> str:
    schema = json.loads(database.schema_json)
    aliases = json.loads(database.routing_aliases_json)
    return " ".join([database.name, database.description or "", database.category or "", *(aliases), *(field.get("name", "") for field in schema)]).lower()


def route_databases(settings: Settings, text: str) -> tuple[list[dict], str | None, float]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    with get_session(settings) as session:
        databases = session.scalars(select(SupplierDatabaseRow).where(SupplierDatabaseRow.status == "active").order_by(SupplierDatabaseRow.created_at)).all()
    routing = []
    for database in databases:
        db_text = _database_text(database)
        unique_words = {word for word in words if len(word) >= 4}
        score = sum(1 for word in unique_words if word in db_text)
        routing.append({"database_id": database.id, "name": database.name, "score": score,
                        "reason": "Name, description, category, aliases, or schema matched quotation text" if score else "No strong lexical match found"})
    routing.sort(key=lambda item: item["score"], reverse=True)
    if not routing:
        return routing, None, 0.0
    top = routing[0]
    second_score = routing[1]["score"] if len(routing) > 1 else 0
    if top["score"] <= 0 or (top["score"] == second_score and len(routing) > 1):
        return routing, None, float(top["score"])
    return routing, top["database_id"], float(top["score"])


def _normalize_aliases(field: dict) -> list[str]:
    names = [field["name"].replace("_", " "), field.get("label", "")]
    names.extend(field.get("aliases", []) or [])
    return list(dict.fromkeys(name.strip().lower() for name in names if name.strip()))


def _extract_field(field: dict, text: str) -> tuple[str | None, str | None]:
    lowered = text.lower()
    aliases = _normalize_aliases(field)
    for alias in sorted(aliases, key=len, reverse=True):
        escaped = re.escape(alias)
        patterns = [
            rf"(?im)^\s*{escaped}\s*[:=\-]\s*(.+?)\s*$",
            rf"(?is)\b{escaped}\b[^\n\d]{{0,20}}([€£$¥]?\s*[\d][\d,\.]*(?:\s*/\s*[A-Za-z]+)?)",
            rf"(?is)\b{escaped}\b[^\n:{{0,40}}]{{0,40}}[:\-=]\s*([^\n]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = match.group(1).strip().strip(".;")
                if value:
                    snippet = text[max(0, match.start()-40):min(len(text), match.end()+40)].replace("\n", " ").strip()
                    return value, snippet[:300]
    return None, None


def _validate_field(field: dict, value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    field_type = field.get("type", "text")
    if field_type in {"number", "currency"}:
        numeric_match = re.search(r"(?:[€£$¥]\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)", value)
        if not numeric_match:
            return None
        candidate = numeric_match.group(1).replace(",", "")
        try:
            Decimal(candidate)
        except InvalidOperation:
            return None
        value = candidate
    if field_type == "date":
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return None
    return value


def list_actions(settings: Settings) -> list[dict]:
    with get_session(settings) as session:
        actions = session.scalars(select(SupplierQuotationActionRow).order_by(SupplierQuotationActionRow.created_at.desc())).all()
        database_ids = {action.database_id for action in actions if action.database_id}
        names = {row.id: row.name for row in session.scalars(select(SupplierDatabaseRow).where(SupplierDatabaseRow.id.in_(database_ids))).all()} if database_ids else {}
        intake_ids = {action.intake_id for action in actions}
        from ..db.models import IntakeItemRow
        files = {row.id: row.original_filename for row in session.scalars(select(IntakeItemRow).where(IntakeItemRow.id.in_(intake_ids))).all()} if intake_ids else {}
    return [{"id": action.id, "intake_id": action.intake_id, "file": files.get(action.intake_id),
             "database_id": action.database_id, "database_name": names.get(action.database_id) if action.database_id else None,
             "status": action.status, "routing": json.loads(action.routing_json),
             "proposed_row": json.loads(action.proposed_row_json), "missing_fields": json.loads(action.missing_fields_json),
             "evidence": json.loads(action.evidence_json), "notes": action.notes,
             "created_csv_row": json.loads(action.created_csv_row_json) if action.created_csv_row_json else None,
             "created_at": action.created_at, "updated_at": action.updated_at}
            for action in actions]


def get_action(settings: Settings, action_id: str) -> dict:
    for action in list_actions(settings):
        if action["id"] == action_id:
            return action
    raise NotFoundError("supplier quotation action not found")


def process_supplier_quotation(settings: Settings, intake_id: str, database_id: str | None = None) -> dict:
    from ..db.models import IntakeItemRow
    with get_session(settings) as session:
        intake = session.get(IntakeItemRow, intake_id)
        if intake is None:
            raise NotFoundError("quotation document not found")
        text = intake.raw_text
        if database_id:
            database = session.get(SupplierDatabaseRow, database_id)
            if database is None or database.status != "active":
                raise NotFoundError("supplier database not found")
            routing = [{"database_id": database.id, "name": database.name, "score": 1.0, "reason": "Confirmed manually for this quotation"}]
            chosen_id = database.id
        else:
            routing, chosen_id, _ = route_databases(settings, text)
            database = session.get(SupplierDatabaseRow, chosen_id) if chosen_id else None

        now = utcnow_iso()
        existing = session.scalar(select(SupplierQuotationActionRow).where(SupplierQuotationActionRow.intake_id == intake_id))
        if existing:
            action = existing
        else:
            action = SupplierQuotationActionRow(id=str(uuid4()), intake_id=intake_id, created_at=now)

        if database is None:
            action.database_id = None
            action.status = "needs_human_review"
            action.proposed_row_json = "{}"
            action.missing_fields_json = "[]"
            action.evidence_json = "{}"
            action.notes = "No unique database could be identified. Confirm a connected supplier database before analysis."
            action.routing_json = json.dumps(routing)
            action.updated_at = now
            session.add(action)
            session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_quotation_action", entity_id=action.id,
                                      action="database_routing_pending", details_json=json.dumps(routing), created_at=now))
            session.commit()
            return get_action(settings, action.id)

        schema = json.loads(database.schema_json)
        proposed: dict[str, str] = {}
        evidence: dict[str, str] = {}
        missing: list[dict[str, str]] = []
        for field in schema:
            value, snippet = _extract_field(field, text)
            value = _validate_field(field, value)
            if value:
                proposed[field["name"]] = value
                evidence[field["name"]] = snippet or "Source value found in quotation"
            elif field.get("required"):
                missing.append({"field": field["name"], "label": field.get("label", field["name"]),
                                "reason": "Required by this supplier database schema and not grounded in the quotation"})

        if missing:
            status = "needs_clarification"
            notes = "Required supplier database fields are missing or unclear. No CSV record will be created."
        elif not proposed:
            status = "needs_human_review"
            notes = "No schema fields could be grounded in the quotation; human review is required."
        else:
            status = "ready_for_approval"
            notes = "A complete schema-driven candidate row is ready for explicit approval."
        action.database_id = database.id
        action.status = status
        action.routing_json = json.dumps(routing)
        action.proposed_row_json = json.dumps(proposed)
        action.missing_fields_json = json.dumps(missing)
        action.evidence_json = json.dumps(evidence)
        action.notes = notes
        action.created_csv_row_json = None
        action.updated_at = now
        session.add(action)
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_quotation_action", entity_id=action.id,
                                  action="proposed", details_json=json.dumps({"database_id": database.id, "missing_count": len(missing), "status": status}), created_at=now))
        session.commit()
        return get_action(settings, action.id)


def update_proposal(settings: Settings, action_id: str, proposed_row: dict[str, str], notes: str | None = None) -> dict:
    with get_session(settings) as session:
        action = session.get(SupplierQuotationActionRow, action_id)
        if action is None:
            raise NotFoundError("supplier quotation action not found")
        if action.status == "record_created":
            raise ValidationFailureError("an approved record cannot be changed after CSV creation")
        action.proposed_row_json = json.dumps(proposed_row)
        action.notes = notes or action.notes
        action.updated_at = utcnow_iso()
        session.add(action)
        session.commit()
    return get_action(settings, action_id)


def _append_row(settings: Settings, database: SupplierDatabaseRow, row: dict[str, str]) -> tuple[dict[str, str], str]:
    path = Path(database.csv_path)
    if not path.is_file():
        raise NotFoundError("connected CSV file is missing")
    headers, existing = _read_csv(path)
    schema_names = [field["name"] for field in json.loads(database.schema_json)]
    if set(headers) != set(schema_names):
        raise ValidationFailureError("connected CSV header no longer matches its saved schema")
    normalized = {header: str(row.get(header) or "").strip() for header in headers}
    normalized_key = tuple(normalized[name] for name in headers)
    for record in existing:
        if tuple(str(record.get(name) or "").strip() for name in headers) == normalized_key:
            raise ValidationFailureError("this exact supplier record is already present in the CSV")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(path.stem + f".backup-{timestamp}" + path.suffix)
    shutil.copy2(path, backup)
    try:
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            if path.stat().st_size == 0:
                writer.writeheader()
            writer.writerow(normalized)
            handle.flush()
    except Exception:
        shutil.copy2(backup, path)
        raise
    return normalized, str(backup)


def approve_action(settings: Settings, action_id: str, corrected_row: dict[str, str] | None = None, notes: str | None = None) -> dict:
    with get_session(settings) as session:
        action = session.get(SupplierQuotationActionRow, action_id)
        if action is None:
            raise NotFoundError("supplier quotation action not found")
        if action.status == "record_created":
            return get_action(settings, action_id)
        if not action.database_id:
            raise ValidationFailureError("confirm a supplier database before approval")
        database = session.get(SupplierDatabaseRow, action.database_id)
        if database is None or database.status != "active":
            raise NotFoundError("supplier database not found")
        proposed = json.loads(action.proposed_row_json)
        if corrected_row:
            proposed.update(corrected_row)
        missing = json.loads(action.missing_fields_json)
        if missing:
            schema_names = {field["name"]: field for field in json.loads(database.schema_json)}
            unresolved = [item for item in missing if item["field"] in schema_names and not str(proposed.get(item["field"] or "") if item["field"] else "").strip()]
            if unresolved:
                raise ValidationFailureError("required fields remain unresolved: " + ", ".join(item["label"] for item in unresolved))
        normalized, backup = _append_row(settings, database, proposed)
        now = utcnow_iso()
        action.status = "record_created"
        action.proposed_row_json = json.dumps(normalized)
        action.created_csv_row_json = json.dumps(normalized)
        action.notes = notes or "Record approved and appended to the connected CSV after human approval."
        action.updated_at = now
        database.record_count = _read_csv(Path(database.csv_path))[1].__len__()
        database.updated_at = now
        session.add(database)
        session.add(action)
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_quotation_action", entity_id=action.id,
                                  action="csv_record_created", details_json=json.dumps({"backup": backup, "row": normalized}), created_at=now))
        session.commit()
    return get_action(settings, action_id)


def set_action_status(settings: Settings, action_id: str, status: str, notes: str | None = None) -> dict:
    allowed = {"needs_clarification", "needs_human_review", "ready_for_approval", "rejected"}
    if status not in allowed:
        raise ValidationFailureError("unsupported supplier quotation status")
    with get_session(settings) as session:
        action = session.get(SupplierQuotationActionRow, action_id)
        if action is None:
            raise NotFoundError("supplier quotation action not found")
        if action.status == "record_created":
            raise ValidationFailureError("a created record cannot change status")
        action.status = status
        action.notes = notes or action.notes
        action.updated_at = utcnow_iso()
        session.add(action)
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_quotation_action", entity_id=action.id,
                                  action="status_changed", details_json=json.dumps({"status": status}), created_at=action.updated_at))
        session.commit()
    return get_action(settings, action_id)


def _numeric(value: str | None) -> Decimal | None:
    if value is None:
        return None
    candidate = re.sub(r"^[€£$¥]", "", value.replace(",", "").strip())
    candidate = candidate.split("/")[0].strip()
    try:
        return Decimal(candidate)
    except InvalidOperation:
        return None


def chat_supplier_databases(settings: Settings, question: str, database_id: str | None = None) -> dict:
    question_l = question.lower().strip()
    with get_session(settings) as session:
        query = select(SupplierDatabaseRow).where(SupplierDatabaseRow.status == "active")
        if database_id:
            query = query.where(SupplierDatabaseRow.id == database_id)
        databases = session.scalars(query.order_by(SupplierDatabaseRow.name)).all()
    citations = []
    if any(word in question_l for word in ("cheapest", "lowest", "minimum", "min ")):
        candidates = []
        for database in databases:
            headers, rows = _read_csv(Path(database.csv_path))
            numeric_fields = [field for field in json.loads(database.schema_json)
                              if field.get("type") == "currency"
                              or "price" in str(field.get("name", "")).lower()
                              or "rate" in str(field.get("name", "")).lower()
                              or "cost" in str(field.get("name", "")).lower()]
            for field in numeric_fields:
                for row_index, row in enumerate(rows, start=2):
                    number = _numeric(row.get(field["name"]))
                    if number is None:
                        continue
                    candidates.append((number, database, field, row_index, row))
        if candidates:
            number, database, field, row_index, row = min(candidates, key=lambda item: item[0])
            citations = [{"database_id": database.id, "database_name": database.name,
                          "csv_row": row_index, "row": row}]
            answer = f"The lowest {field['label']} found is {number} in {database.name}. The matching supplier record is on CSV row {row_index}."
        else:
            answer = "No numeric price or rate field was available to compare."
    elif any(word in question_l for word in ("highest", "most expensive", "maximum", "expensive")):
        candidates = []
        for database in databases:
            headers, rows = _read_csv(Path(database.csv_path))
            for field in json.loads(database.schema_json):
                field_name = str(field.get("name", "")).lower()
                if field.get("type") != "currency" and not any(word in field_name for word in ("price", "rate", "cost")):
                    continue
                for row_index, row in enumerate(rows, start=2):
                    number = _numeric(row.get(field["name"]))
                    if number is not None:
                        candidates.append((number, database, field, row_index, row))
        if candidates:
            number, database, field, row_index, row = max(candidates, key=lambda item: item[0])
            citations = [{"database_id": database.id, "database_name": database.name, "csv_row": row_index, "row": row}]
            answer = f"The highest {field['label']} found is {number} in {database.name}. The matching supplier record is on CSV row {row_index}."
        else:
            answer = "No numeric price or rate field was available to compare."
    elif any(word in question_l for word in ("last added", "latest supplier", "most recent", "last supplier", "recently added")):
        latest_database = databases[-1] if databases else None
        if latest_database:
            _, rows = _read_csv(Path(latest_database.csv_path))
            if rows:
                citations = [{"database_id": latest_database.id, "database_name": latest_database.name,
                              "csv_row": len(rows) + 1, "row": rows[-1]}]
                answer = f"The most recent supplier record currently available in connected databases is the last row in {latest_database.name}."
            else:
                answer = f"{latest_database.name} has no supplier records yet."
        else:
            answer = "No supplier databases are connected."
    else:
        matches = []
        terms = [term for term in re.findall(r"[a-z0-9]+", question_l) if len(term) >= 4]
        for database in databases:
            _, rows = _read_csv(Path(database.csv_path))
            for row_index, row in enumerate(rows, start=2):
                haystack = " ".join(str(value) for value in row.values()).lower()
                if terms and sum(term in haystack for term in terms) >= 1:
                    matches.append((database, row_index, row))
        matches = matches[:20]
        citations = [{"database_id": database.id, "database_name": database.name,
                      "csv_row": row_index, "row": row} for database, row_index, row in matches]
        if matches:
            answer = f"Found {len(matches)} supplier record(s) matching words in the question. Review the cited records for exact qualification."
        else:
            answer = "No directly matching supplier records were found. Ask for a supplier name, price, date, unit, or status to narrow the search."

    now = utcnow_iso()
    with get_session(settings) as session:
        chat = SupplierChatRow(id=str(uuid4()), question=question, answer=answer,
                               citations_json=json.dumps(citations), created_at=now)
        session.add(chat)
        session.commit()
    return {"question": question, "answer": answer, "citations": citations, "created_at": now}


def chat_history(settings: Settings, limit: int = 30) -> list[dict]:
    with get_session(settings) as session:
        rows = session.scalars(select(SupplierChatRow).order_by(SupplierChatRow.created_at.desc()).limit(max(1, min(limit, 100)))).all()
    return [{"id": row.id, "question": row.question, "answer": row.answer,
             "citations": json.loads(row.citations_json), "created_at": row.created_at} for row in rows]

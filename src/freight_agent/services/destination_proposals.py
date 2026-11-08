"""Automatic routing and independent, evidence-grounded extraction per destination."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from pathlib import Path
from sqlalchemy import select, text
from ..agents.destination_extractor import run_extraction
from ..config import Settings
from ..db.engine import get_session
from ..db.models import DestinationProposalRow, SupplierDatabaseRow, IntakeItemRow, AuditEventRow, utcnow_iso
from ..domain.exceptions import NotFoundError, ValidationFailureError



def validated(field: dict, value: str) -> str | None:
    from .supplier_workflow import _validate_field
    value = value.strip()
    if _validate_field(field, value) is None:
        return None
    kind = field.get("type", "text")
    if kind in {"number", "currency"}:
        candidate = re.sub(r"^[€£$¥]\s*", "", value).replace(",", "")
        try:
            amount = Decimal(candidate)
        except InvalidOperation:
            return None
        return str(amount) if amount.is_finite() else None
    if kind == "date":
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    if kind == "boolean":
        return {"yes": "yes", "true": "yes", "y": "yes", "no": "no", "false": "no", "n": "no"}.get(value.lower())
    if "email" in field["name"].lower() and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        return None
    return value


def extract(settings: Settings, schema: list[dict], source: str) -> dict:
    from .supplier_workflow import _normalize_aliases, _validate_field
    values, evidence = {}, {}
    issues = []
    # Explicit labelled values are deterministic. The local model handles prose.
    for field in schema:
        matches = []
        for alias in _normalize_aliases(field):
            matches.extend(re.finditer(r"(?im)^\s*" + re.escape(alias) + r"\s*[:=]\s*([^\n]+)", source))
        candidates = {match.group(1).strip(): match.group(0).strip() for match in matches}
        if len(candidates) > 1:
            issues.append("Conflicting source values: " + field["name"])
        if len(candidates) == 1:
            value, snippet = next(iter(candidates.items()))
            valid = validated(field, value)
            if valid is not None:
                values[field["name"]] = valid
                evidence[field["name"]] = snippet
    remaining = [field for field in schema if field["name"] not in values]
    if remaining:
        output = run_extraction(settings, remaining, source)
        issues.extend(output.issues)
        allowed = {field["name"]: field for field in remaining}
        seen = set()
        for item in output.fields:
            if item.field not in allowed or item.field in seen:
                issues.append("Unexpected or repeated field: " + item.field)
                continue
            seen.add(item.field)
            if item.value is None:
                continue
            snippet = item.snippet or ""
            compact = lambda value: re.sub(r"[\s,]", "", value).casefold()
            valid = validated(allowed[item.field], item.value)
            if not snippet or snippet not in source or compact(item.value) not in compact(snippet) or valid is None:
                issues.append("Ungrounded or invalid value: " + item.field)
                continue
            values[item.field] = valid
            evidence[item.field] = snippet
    missing = [field["name"] for field in schema if field.get("required") and not values.get(field["name"])]
    return {"values": {field["name"]: values.get(field["name"]) for field in schema},
            "evidence": evidence, "missing": missing, "issues": issues,
            "questions": ["Please provide " + field.get("label", field["name"]) + "." for field in schema if field["name"] in missing]}


def payload(row: DestinationProposalRow, database: SupplierDatabaseRow, intake: IntakeItemRow) -> dict:
    return {"id": row.id, "intake_id": row.intake_id, "database_id": row.database_id,
        "database_name": database.name, "source_name": intake.original_filename,
        "status": row.status, "schema": json.loads(row.schema_json), **json.loads(row.result_json)}


def list_proposals(settings: Settings) -> list[dict]:
    with get_session(settings) as session:
        return [payload(row, database, intake) for row, database, intake in session.execute(
            select(DestinationProposalRow, SupplierDatabaseRow, IntakeItemRow)
            .join(SupplierDatabaseRow, DestinationProposalRow.database_id == SupplierDatabaseRow.id)
            .join(IntakeItemRow, DestinationProposalRow.intake_id == IntakeItemRow.id)
            .order_by(DestinationProposalRow.created_at.desc())).all()]


def process(settings: Settings, intake_id: str, database_id: str | None = None, progress=None, target_ids: list[str] | None = None) -> dict:
    from .supplier_workflow import route_databases
    with get_session(settings) as session:
        intake = session.get(IntakeItemRow, intake_id)
        if not intake:
            raise NotFoundError("Source document not found")
        source = intake.raw_text
        source_hash = hashlib.sha256(source.encode()).hexdigest()
        from ..db.models import SourceDocumentRow
        parse_errors = session.query(SourceDocumentRow).filter_by(intake_item_id=intake_id).filter(SourceDocumentRow.parse_error.is_not(None)).all()
        if parse_errors:
            intake.processing_status = "needs_review"
            session.commit()
            failures = [f"{item.filename}: {item.parse_error}" for item in parse_errors]
            return {"action": {"id": None, "intake_id": intake_id, "status": "attachment_unreadable"},
                    "actions": [], "proposals": [], "draft": None, "notes": failures}
        # Verify the message is a quotation before matching any destination schema.
        from .classification import classify_intake
        from ..domain.enums import ClassificationStatus
        classified, verdict = classify_intake(settings, intake_id)
        if classified.classification_status != ClassificationStatus.relevant:
            classified.processing_status = "processed" if classified.classification_status == ClassificationStatus.irrelevant else "needs_review"
            session.add(intake)
            intake.classification_status = classified.classification_status
            intake.processing_status = classified.processing_status
            session.commit()
            status = "not_a_quotation" if classified.classification_status == ClassificationStatus.irrelevant else "needs_review"
            return {"action": {"id": None, "intake_id": intake_id, "status": status}, "actions": [], "proposals": [], "draft": None,
                    "notes": ["Email classified as not a quotation." if status == "not_a_quotation" else "Email type is uncertain; it was set aside for review."]}
        routing, _, _ = route_databases(settings, source)
        targets = [item["database_id"] for item in routing if item["score"] > 0]
        if target_ids is not None:
            targets = target_ids
        if database_id:
            targets = [database_id]
        databases = []
        for target in targets:
            database = session.get(SupplierDatabaseRow, target)
            if database and database.status == "active":
                databases.append((database.id, database.schema_json, database.name))
        intake.processing_status = "processing" if databases else "needs_review"
        if not databases:
            considered = "; ".join(item["name"] + ": " + item["reason"] for item in routing)
            intake.classification_reason = "Verified quotation, but no destination matched. " + (considered or "No active supplier databases are configured.")
        session.commit()
    results = []
    for target, schema_json, name in databases:
        revision_key = hashlib.sha256((source_hash + schema_json).encode()).hexdigest()
        if progress:
            progress("Extracting fields for " + name)
        with get_session(settings) as session:
            old = session.scalar(select(DestinationProposalRow).where(
                DestinationProposalRow.intake_id == intake_id, DestinationProposalRow.database_id == target, DestinationProposalRow.revision_key == revision_key))
            if old and old.source_hash == source_hash and old.schema_json == schema_json and old.status != "failed":
                results.append(payload(old, session.get(SupplierDatabaseRow, target), session.get(IntakeItemRow, intake_id)))
                continue
            if old and old.status == "imported":
                results.append(payload(old, session.get(SupplierDatabaseRow, target), session.get(IntakeItemRow, intake_id)))
                continue
        try:
            if len(source) > settings.max_text_chars:
                raise ValidationFailureError("Source exceeds extraction context limit")
            result = extract(settings, json.loads(schema_json), source)
            from ..db.models import SourceDocumentRow
            with get_session(settings) as check:
                if check.query(SourceDocumentRow).filter(SourceDocumentRow.intake_item_id == intake_id, SourceDocumentRow.parse_error.is_not(None)).count():
                    result["issues"].append("One or more source documents could not be parsed")
            status = "ready_for_approval" if not result["missing"] and not result["issues"] and any(result["values"].values()) else "needs_information"
        except Exception as exc:
            # A failed model call for one destination must not prevent the others.
            result = {"values": {}, "evidence": {}, "missing": [], "issues": ["Extraction failed: " + type(exc).__name__]}
            status = "failed"
        result["routing"] = [item for item in routing if item["database_id"] == target]
        with get_session(settings) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.scalar(select(DestinationProposalRow).where(DestinationProposalRow.intake_id == intake_id,
                DestinationProposalRow.database_id == target, DestinationProposalRow.revision_key == revision_key))
            if row is not None and row.status != "failed":
                results.append(payload(row, session.get(SupplierDatabaseRow, target), session.get(IntakeItemRow, intake_id)))
                continue
            now = utcnow_iso()
            if row is None:
                row = DestinationProposalRow(id=str(uuid4()), intake_id=intake_id, database_id=target, revision_key=revision_key, created_at=now)
                session.add(row)
            row.schema_json, row.source_hash, row.result_json, row.status, row.updated_at = schema_json, source_hash, json.dumps(result), status, now
            session.add(AuditEventRow(id=str(uuid4()), entity_type="destination_proposal", entity_id=row.id,
                action="extracted", details_json=json.dumps({"database_id": target, "status": status}), created_at=now))
            session.commit()
            results.append(payload(row, session.get(SupplierDatabaseRow, target), session.get(IntakeItemRow, intake_id)))
    with get_session(settings) as session:
        session.get(IntakeItemRow, intake_id).processing_status = "processed" if results else "needs_review"
        session.commit()
    first = results[0] if results else {"id": None, "intake_id": intake_id, "database_id": None, "status": "no_destination_match"}
    diagnostic = []
    if not results:
        with get_session(settings) as session:
            item = session.get(IntakeItemRow, intake_id)
            diagnostic = [item.classification_reason or "No active destination database matched this quotation."]
    return {"action": first, "actions": results, "proposals": results, "draft": None, "notes": diagnostic}


def decide(settings: Settings, proposal_id: str, operation: str) -> dict:
    from .supplier_workflow import _append_row
    with get_session(settings) as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(DestinationProposalRow, proposal_id)
        if row is None:
            raise NotFoundError("Proposal not found")
        database = session.get(SupplierDatabaseRow, row.database_id)
        intake = session.get(IntakeItemRow, row.intake_id)
        if operation not in {"approve", "reject", "import"}:
            raise ValidationFailureError("Unknown proposal action")
        if row.status == "imported":
            if operation != "import":
                raise ValidationFailureError("Imported record history is read-only")
            return payload(row, database, intake)
        if database.status != "active" or database.schema_json != row.schema_json or hashlib.sha256(intake.raw_text.encode()).hexdigest() != row.source_hash:
            raise ValidationFailureError("Source or destination schema changed; reprocess before approval")
        result = json.loads(row.result_json)
        if operation == "approve":
            if row.status not in {"ready_for_approval", "approved"}:
                raise ValidationFailureError("Only validated, complete proposals can be approved")
            row.status = "approved"
        elif operation == "reject":
            row.status = "rejected"
        else:
            if row.status != "approved":
                raise ValidationFailureError("Approve this destination record before import")
            values = {key: value or "" for key, value in result["values"].items()}
            _, backup = _append_row(settings, database, values)
            result["backup_filename"] = Path(backup).name
            row.result_json = json.dumps(result)
            row.status = "imported"
            from .supplier_databases import _parse_csv
            database.record_count = len(_parse_csv(Path(database.csv_path))[1])
        row.updated_at = utcnow_iso()
        session.add(AuditEventRow(id=str(uuid4()), entity_type="destination_proposal", entity_id=row.id,
            action=operation, details_json=json.dumps({"database_id": row.database_id}), created_at=row.updated_at))
        session.commit()
        return payload(row, database, intake)

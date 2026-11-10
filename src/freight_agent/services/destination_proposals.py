"""Automatic routing and independent, evidence-grounded extraction per destination."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from pathlib import Path
from sqlalchemy import select, text
from ..agents.destination_extractor import run_extraction
from ..config import Settings
from ..db.engine import get_session
from ..db.models import DestinationProposalRow, SupplierDatabaseRow, IntakeItemRow, AuditEventRow, utcnow_iso
from ..domain.exceptions import NotFoundError, ValidationFailureError

logger = logging.getLogger("freight_agent.destination_proposals")
DESTINATION_EXTRACTION_VERSION = "2026-11-09.3"


def _explicit_not_applicable(value: str) -> bool:
    return bool(re.search(r"\b(?:not applicable|does not apply|doesn't apply|not relevant|n\s*/\s*a)\b", value, re.I))



def validated(field: dict, value: str) -> str | None:
    from .supplier_workflow import _validate_field
    value = value.strip()
    normalized_value = _validate_field(field, value)
    if normalized_value is None:
        return None
    value = normalized_value
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
            for pattern in ("%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y"):
                try:
                    return datetime.strptime(value, pattern).date().isoformat()
                except ValueError:
                    continue
            return None
    if kind == "boolean":
        return {"yes": "yes", "true": "yes", "y": "yes", "no": "no", "false": "no", "n": "no"}.get(value.lower())
    if "email" in field["name"].lower() and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        return None
    return value


def extract(settings: Settings, schema: list[dict], source: str) -> dict:
    from .supplier_workflow import _normalize_aliases, _validate_field
    values, evidence, confidence, explanations, not_applicable = {}, {}, {}, {}, set()
    issues = []
    performance_fields = {"otd_rating_pct", "billing_accuracy_pct", "performance_rating",
                          "supplier_performance", "internal_risk_level"}
    normalize_label = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
    source_lines = source.splitlines()
    # Explicit labelled values are deterministic. The local model handles prose.
    for field in schema:
        name = field["name"]
        if name.casefold() in performance_fields or field.get("scope") in {"supplier_master", "system"}:
            continue
        matches = []
        for alias in _normalize_aliases(field):
            matches.extend(re.finditer(r"(?im)^\s*" + re.escape(alias) + r"\s*[:=]\s*([^\n]+)", source))
        candidates = {match.group(1).strip(): match.group(0).strip() for match in matches}
        # PDF parsers often preserve a form as a label line followed by a value
        # line. Accept that shape only when the next non-empty line validates for
        # this field, and retain both lines as the exact evidence excerpt.
        normalized_aliases = {normalize_label(alias) for alias in _normalize_aliases(field)}
        for line_index, line in enumerate(source_lines):
            if normalize_label(line.strip(" \t•*-")) not in normalized_aliases:
                continue
            for candidate_line in source_lines[line_index + 1:line_index + 5]:
                candidate = candidate_line.strip(" \t•*-:")
                if not candidate:
                    continue
                if candidate.startswith("[") and candidate.endswith("]"):
                    continue
                if normalize_label(candidate) in normalized_aliases:
                    break
                valid = validated(field, candidate)
                if valid is not None:
                    candidates.setdefault(candidate, line.strip() + "\n" + candidate)
                    break
        if len(candidates) > 1:
            issues.append("Conflicting source values: " + name)
            candidate, snippet = next(iter(candidates.items()))
            valid = validated(field, candidate)
            if valid is not None:
                values[name] = valid
                evidence[name] = snippet
                confidence[name] = 0.50
                explanations[name] = "The quotation contains conflicting labeled values, so this candidate needs supplier confirmation."
            continue
        if len(candidates) == 1:
            value, snippet = next(iter(candidates.items()))
            if _explicit_not_applicable(value):
                not_applicable.add(name)
                evidence[name] = snippet
                explanations[name] = "The quotation explicitly marks this field as not applicable."
                continue
            valid = validated(field, value)
            if valid is not None:
                values[name] = valid
                evidence[name] = snippet
                confidence[name] = 0.99
                explanations[name] = "Matched a value on an explicitly labeled source line."
    remaining = [field for field in schema if field["name"] not in values]
    remaining = [field for field in remaining
                 if field.get("scope") not in {"supplier_master", "system"}
                 and field["name"].casefold() not in performance_fields]
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
            if item.not_applicable:
                snippet = item.snippet or ""
                if item.value is None and snippet and snippet in source and _explicit_not_applicable(snippet):
                    not_applicable.add(item.field)
                    evidence[item.field] = snippet
                    explanations[item.field] = item.explanation or "This field does not apply to the quoted service."
                    continue
                issues.append("Unsupported not-applicable result: " + item.field)
                continue
            if item.value is None:
                continue
            snippet = item.snippet or ""
            compact = lambda value: re.sub(r"[\s,]", "", value).casefold()
            valid = validated(allowed[item.field], item.value)
            if not snippet or snippet not in source or compact(item.value) not in compact(snippet) or valid is None:
                issues.append("Ungrounded or invalid value: " + item.field)
                explanations[item.field] = item.explanation or "The candidate could not be supported by an exact source excerpt."
                continue
            values[item.field] = valid
            evidence[item.field] = snippet
            score = 0.50 if item.confidence is None else float(item.confidence)
            confidence[item.field] = score
            explanations[item.field] = item.explanation or (
                "The value is grounded, but the extractor did not return a confidence score; supplier confirmation is required."
                if item.confidence is None else
                "Confidence is high and the value is grounded in source text." if score >= 0.70 else
                "The model marked this source interpretation as uncertain.")
    # Port fields do not apply to air/road services. Mark this deterministically from
    # the extracted transport mode, rather than presenting a false missing-data alert.
    transport_mode = str(values.get("Transport_Mode") or "").casefold()
    mode_aliases = {"road": ("road", "truck", "drayage"), "air": ("air",),
                    "ocean": ("ocean", "sea", "maritime"), "rail": ("rail", "train")}
    for field in schema:
        name = field["name"]
        not_applicable_for = {str(mode).casefold() for mode in field.get("not_applicable_for", [])}
        matching_modes = {mode for mode, aliases in mode_aliases.items()
                          if any(alias in transport_mode for alias in aliases)}
        if matching_modes & not_applicable_for and not values.get(name):
            not_applicable.add(name)
            explanations[name] = f"Not applicable to {transport_mode or 'this'} transport."
    conflicting = {issue.rsplit(": ", 1)[-1] for issue in issues if issue.startswith("Conflicting source values:")}
    uncertain = sorted(name for name, score in confidence.items()
                       if score is None or score < 0.70 or name in conflicting)
    missing = [field["name"] for field in schema if field.get("required")
               and field.get("scope") not in {"supplier_master", "system"}
               and not values.get(field["name"]) and field["name"] not in not_applicable]
    statuses = {}
    for field in schema:
        name = field["name"]
        if field.get("scope") == "supplier_master":
            statuses[name] = "supplier_master"
        elif field.get("scope") == "system":
            statuses[name] = "system_metadata"
        elif name in not_applicable:
            statuses[name] = "not_applicable"
        elif name in missing:
            statuses[name] = "missing"
        elif name in uncertain:
            statuses[name] = "uncertain"
        elif name in values:
            statuses[name] = "high_confidence"
        else:
            statuses[name] = "not_stated"
    return {"values": {field["name"]: values.get(field["name"]) for field in schema},
            "evidence": evidence, "confidence": confidence, "explanations": explanations,
            "field_status": statuses, "not_applicable": sorted(not_applicable),
            "missing": missing, "uncertain": uncertain, "issues": issues,
            "questions": ["Please provide " + field.get("label", field["name"]) + "." for field in schema if field["name"] in missing]}


def payload(row: DestinationProposalRow, database: SupplierDatabaseRow, intake: IntakeItemRow) -> dict:
    return {"id": row.id, "intake_id": row.intake_id, "database_id": row.database_id,
        "database_name": database.name, "source_name": intake.original_filename,
        "status": row.status, "schema": json.loads(row.schema_json),
        "database_active": database.status == "active",
        "schema_current": database.status == "active" and
                          json.loads(row.schema_json) == json.loads(database.schema_json),
        "created_at": row.created_at, "updated_at": row.updated_at,
        **json.loads(row.result_json)}


def list_proposals(settings: Settings) -> list[dict]:
    # Keep the dashboard's schema-current indicator honest even when the first
    # page opened after an upgrade is Quotations rather than Supplier settings.
    from .supplier_workflow import route_databases
    route_databases(settings, "")
    with get_session(settings) as session:
        rows = session.execute(
            select(DestinationProposalRow, SupplierDatabaseRow, IntakeItemRow)
            .join(SupplierDatabaseRow, DestinationProposalRow.database_id == SupplierDatabaseRow.id)
            .join(IntakeItemRow, DestinationProposalRow.intake_id == IntakeItemRow.id)
            .order_by(DestinationProposalRow.created_at.desc())).all()
        latest = {}
        for row, database, intake in rows:
            latest.setdefault(row.intake_id, payload(row, database, intake))
        return list(latest.values())


def get_proposal(settings: Settings, proposal_id: str) -> dict:
    with get_session(settings) as session:
        row = session.get(DestinationProposalRow, proposal_id)
        if row is None:
            raise NotFoundError("Quotation proposal not found")
        database = session.get(SupplierDatabaseRow, row.database_id)
        intake = session.get(IntakeItemRow, row.intake_id)
        current = payload(row, database, intake)
        previous = session.execute(select(DestinationProposalRow, SupplierDatabaseRow).join(
            SupplierDatabaseRow, DestinationProposalRow.database_id == SupplierDatabaseRow.id
        ).where(DestinationProposalRow.intake_id == row.intake_id,
                DestinationProposalRow.id != row.id
        ).order_by(DestinationProposalRow.created_at.desc())).all()
        current["previous_versions"] = [{"id": item.id, "status": item.status,
            "database_name": old_database.name, "created_at": item.created_at,
            "updated_at": item.updated_at,
            "schema_current": old_database.status == "active" and
                              json.loads(item.schema_json) == json.loads(old_database.schema_json)}
            for item, old_database in previous]
        return current


def reanalyze(settings: Settings, proposal_id: str, progress=None) -> dict:
    """Re-run an old proposal against today's canonical schema; never approve/import it."""
    with get_session(settings) as session:
        row = session.get(DestinationProposalRow, proposal_id)
        if row is None:
            raise NotFoundError("Quotation proposal not found")
        database = session.get(SupplierDatabaseRow, row.database_id)
        intake_id = row.intake_id
        database_id = row.database_id if database and database.status == "active" else None
    return process(settings, intake_id, database_id, progress)


def process_reply(settings: Settings, proposal_id: str, message_id: str, reply_text: str,
                  source_name: str = "Supplier reply") -> dict:
    """Apply a supplier answer to its original proposal and retain reply evidence."""
    from ..db.models import GmailMessageRow

    with get_session(settings) as session:
        row = session.get(DestinationProposalRow, proposal_id)
        message = session.get(GmailMessageRow, message_id)
        if row is None or message is None:
            raise NotFoundError("Original quotation or supplier reply not found")
        original = json.loads(row.result_json or "{}")
        schema = json.loads(row.schema_json or "[]")
        awaiting = set(original.get("missing", [])) | set(original.get("uncertain", []))
        targets = [field for field in schema if field.get("name") in awaiting and
                   field.get("scope") not in {"supplier_master", "system"}]
        intake_id = row.intake_id
        current_status = row.status
    if not targets or not reply_text.strip():
        outcome = {"updated_fields": [], "status": current_status, "drafts": []}
    else:
        reply_result = extract(settings, targets, reply_text)
        now = utcnow_iso()
        updated_fields: list[str] = []
        with get_session(settings) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(DestinationProposalRow, proposal_id)
            if row is None:
                raise NotFoundError("Original quotation proposal not found")
            current = json.loads(row.result_json or "{}")
            values = current.setdefault("values", {})
            evidence = current.setdefault("evidence", {})
            confidence = current.setdefault("confidence", {})
            explanations = current.setdefault("explanations", {})
            statuses = current.setdefault("field_status", {})
            history = current.setdefault("evidence_history", {})
            for name, value in reply_result.get("values", {}).items():
                if value is None:
                    continue
                snippet = reply_result.get("evidence", {}).get(name)
                score = reply_result.get("confidence", {}).get(name)
                if not snippet or snippet not in reply_text:
                    continue
                score = 0.50 if score is None else float(score)
                previous = values.get(name)
                values[name] = value
                evidence[name] = snippet
                confidence[name] = score
                explanations[name] = reply_result.get("explanations", {}).get(name) or \
                    "Supplier reply explicitly supplied this value; confidence is grounded in the reply excerpt."
                statuses[name] = "high_confidence" if score >= 0.70 else "uncertain"
                history.setdefault(name, []).append({"filename": source_name, "snippet": snippet,
                    "confidence": score, "received_at": message.sent_at or now,
                    "source_type": "supplier_reply", "gmail_message_id": message.id,
                    "previous_value": previous if previous != value else None})
                updated_fields.append(name)
            for name in reply_result.get("not_applicable", []):
                snippet = reply_result.get("evidence", {}).get(name)
                if not snippet or snippet not in reply_text or not _explicit_not_applicable(snippet):
                    continue
                previous = values.pop(name, None)
                confidence.pop(name, None)
                statuses[name] = "not_applicable"
                evidence[name] = snippet
                current.setdefault("not_applicable", [])
                if name not in current["not_applicable"]:
                    current["not_applicable"].append(name)
                explanations[name] = reply_result.get("explanations", {}).get(name) or "Supplier confirmed this field is not applicable."
                history.setdefault(name, []).append({"filename": source_name, "snippet": snippet,
                    "confidence": None, "received_at": message.sent_at or now,
                    "source_type": "supplier_reply", "gmail_message_id": message.id,
                    "status": "not_applicable", "previous_value": previous})
                updated_fields.append(name)
            resolved = set(updated_fields)
            current["uncertain"] = sorted(name for name, score in confidence.items()
                                          if score is None or score < 0.70)
            required = {field["name"] for field in schema if field.get("required") and
                        field.get("scope") not in {"supplier_master", "system"}}
            not_applicable = set(current.get("not_applicable", []))
            current["missing"] = sorted(name for name in required if not values.get(name) and name not in not_applicable)
            for field in schema:
                name = field["name"]
                if field.get("scope") == "supplier_master":
                    statuses[name] = "supplier_master"
                elif field.get("scope") == "system":
                    statuses[name] = "system_metadata"
                elif name in not_applicable:
                    statuses[name] = "not_applicable"
                elif name in current["missing"]:
                    statuses[name] = "missing"
                elif name in current["uncertain"]:
                    statuses[name] = "uncertain"
                elif values.get(name):
                    statuses[name] = "high_confidence"
                else:
                    statuses.setdefault(name, "not_stated")
            current["issues"] = [issue for issue in current.get("issues", [])
                                  if not any(issue.endswith(": " + name) for name in resolved)]
            current.setdefault("reply_history", []).append({"gmail_message_id": message.id,
                "gmail_thread_id": message.gmail_thread_id, "source_name": source_name,
                "received_at": message.sent_at or now, "text": reply_text,
                "updated_fields": updated_fields, "extraction_issues": reply_result.get("issues", [])})
            current["questions"] = ["Please provide " + field.get("label", field["name"]) + "."
                                    for field in schema if field["name"] in current["missing"]]
            current["values"]["Extraction_Confidence_JSON"] = json.dumps(confidence, ensure_ascii=False)
            current["values"]["Extraction_Evidence_JSON"] = json.dumps(history, ensure_ascii=False)
            current["values"]["Extraction_Notes"] = "; ".join(current.get("issues", []))
            status = "ready_for_approval" if not current["missing"] and not current["uncertain"] and not current.get("issues") else "needs_information"
            row.result_json = json.dumps(current, ensure_ascii=False)
            row.status = status
            row.updated_at = now
            intake = session.get(IntakeItemRow, intake_id)
            if intake:
                intake.processing_status = "processed" if status == "ready_for_approval" else "needs_review"
            session.add(AuditEventRow(id=str(uuid4()), entity_type="destination_proposal", entity_id=row.id,
                action="supplier_reply_processed", details_json=json.dumps({"gmail_message_id": message.id,
                    "updated_fields": updated_fields, "status": status}), created_at=now))
            session.commit()
            outcome = {"updated_fields": updated_fields, "status": status}
        try:
            with get_session(settings) as session:
                row = session.get(DestinationProposalRow, proposal_id)
                database = session.get(SupplierDatabaseRow, row.database_id)
                intake = session.get(IntakeItemRow, row.intake_id)
                refreshed = payload(row, database, intake)
            outcome["drafts"] = __import__("freight_agent.services.supplier_workflow", fromlist=[
                "generate_proposal_clarification_drafts"]).generate_proposal_clarification_drafts(
                    settings, proposal_id, refreshed)
        except Exception as exc:  # a supplier reply is retained even if draft preparation fails
            logger.warning("follow-up clarification drafts were not prepared for proposal %s: %s",
                           proposal_id, type(exc).__name__)
            outcome["drafts"] = []
    return outcome


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
        classification_status = str(intake.classification_status)
        if classification_status == ClassificationStatus.relevant:
            # Reprocessing an already verified quotation must not silently replace
            # its classification verdict with a new probabilistic answer.
            classified, verdict = intake, None
        elif classification_status in {ClassificationStatus.needs_review, ClassificationStatus.irrelevant}:
            # Preserve the classification gate. A user can explicitly resolve it
            # through the review flow; destination extraction does not override it.
            classified, verdict = intake, None
        else:
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
        revision_key = hashlib.sha256(
            (source_hash + schema_json + DESTINATION_EXTRACTION_VERSION).encode()).hexdigest()
        if progress:
            progress("Extracting fields for " + name)
        with get_session(settings) as session:
            previous = session.scalar(select(DestinationProposalRow).where(
                DestinationProposalRow.intake_id == intake_id,
                DestinationProposalRow.database_id == target,
            ).order_by(DestinationProposalRow.created_at.desc()).limit(1))
            previous_values = json.loads(previous.result_json or "{}").get("values", {}) if previous else {}
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
            schema = json.loads(schema_json)
            result = extract(settings, schema, source)
            # Metadata is not quotation evidence. A matched supplier contributes only
            # supplier-level attributes; old quotations never fill a new offer.
            from .supplier_workflow import _match_database_record
            with get_session(settings) as context:
                database_row = context.get(SupplierDatabaseRow, target)
                supplier_values, supplier_evidence, existing_supplier = _match_database_record(
                    schema, result["values"], source, database_row)
                if existing_supplier:
                    for key, value in supplier_values.items():
                        if schema_by_name := next((f for f in schema if f["name"] == key), None):
                            if schema_by_name.get("scope") == "supplier_master":
                                result["values"][key] = value
                                result["evidence"][key] = supplier_evidence.get(key, "Existing supplier master record")
                                result["field_status"][key] = "supplier_master"
                    if not result["values"].get("Supplier_Name") and supplier_values.get("Supplier_Name"):
                        result["values"]["Supplier_Name"] = supplier_values["Supplier_Name"]
                        result["field_status"]["Supplier_Name"] = "supplier_master"
                        result["missing"] = [name for name in result["missing"] if name != "Supplier_Name"]
                elif result["values"].get("Supplier_Name") and not result["values"].get("Supplier_ID"):
                    result["values"]["Supplier_ID"] = "SUP-" + uuid4().hex[:8].upper()
            result["values"].setdefault("Quotation_ID", "QID-" + uuid4().hex[:12].upper())
            if previous_values.get("Quotation_ID"):
                result["values"]["Quotation_ID"] = previous_values["Quotation_ID"]
            with get_session(settings) as context:
                intake_row = context.get(IntakeItemRow, intake_id)
            result["evidence_history"] = {key: [{"filename": intake_row.original_filename if intake_row else "source",
                "snippet": snippet, "confidence": result["confidence"].get(key),
                "status": result["field_status"].get(key), "received_at": utcnow_iso()}]
                for key, snippet in result["evidence"].items()}
            with get_session(settings) as context:
                intake_row = context.get(IntakeItemRow, intake_id)
                received = (intake_row.received_at or intake_row.created_at) if intake_row else None
                if received:
                    result["values"]["Quotation_Received_Date"] = str(received)[:10]
                    result["field_status"]["Quotation_Received_Date"] = "system_metadata"
                    result["explanations"]["Quotation_Received_Date"] = "Taken from the received email/document timestamp, not inferred from quotation text."
                result["values"]["Record_Data_Status"] = "Supplier-provided quotation; not independently verified"
                result["values"]["Data_Source"] = intake_row.original_filename if intake_row else "Inbound quotation source"
                result["values"]["Extraction_Confidence_JSON"] = json.dumps(result["confidence"], ensure_ascii=False)
                result["values"]["Extraction_Evidence_JSON"] = json.dumps(result["evidence_history"], ensure_ascii=False)
                result["values"]["Extraction_Notes"] = "; ".join(result["issues"])
            from ..db.models import SourceDocumentRow
            with get_session(settings) as check:
                if check.query(SourceDocumentRow).filter(SourceDocumentRow.intake_item_id == intake_id, SourceDocumentRow.parse_error.is_not(None)).count():
                    result["issues"].append("One or more source documents could not be parsed")
            status = "ready_for_approval" if not result["missing"] and not result.get("uncertain") and not result["issues"] and any(result["values"].values()) else "needs_information"
        except Exception as exc:
            # A failed model call for one destination must not prevent the others.
            result = {"values": {}, "evidence": {}, "missing": [], "issues": ["Extraction failed: " + type(exc).__name__]}
            status = "failed"
        result["extraction_version"] = DESTINATION_EXTRACTION_VERSION
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
                row = DestinationProposalRow(
                    id=str(uuid4()), intake_id=intake_id, database_id=target,
                    revision_key=revision_key, status=status, schema_json=schema_json,
                    source_hash=source_hash, result_json=json.dumps(result),
                    created_at=now, updated_at=now,
                )
                session.add(row)
                from ..db.models import SupplierEmailDraftRow
                superseded_ids = session.scalars(select(DestinationProposalRow.id).join(
                    SupplierDatabaseRow, DestinationProposalRow.database_id == SupplierDatabaseRow.id
                ).where(DestinationProposalRow.intake_id == intake_id,
                        DestinationProposalRow.id != row.id,
                        (DestinationProposalRow.database_id == target) | (SupplierDatabaseRow.status != "active"))
                ).all()
                if superseded_ids:
                    superseded_drafts = session.scalars(select(SupplierEmailDraftRow).where(
                        SupplierEmailDraftRow.destination_proposal_id.in_(superseded_ids),
                        SupplierEmailDraftRow.status.in_(["draft", "approved"]),
                    )).all()
                    for draft in superseded_drafts:
                        draft.status = "cancelled"
                        draft.updated_at = now
                        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_email_draft",
                            entity_id=draft.id, action="superseded_after_proposal_reanalysis",
                            details_json=json.dumps({"replacement_intake_id": intake_id,
                                                     "replacement_database_id": target}),
                            created_at=now))
            row.schema_json, row.source_hash, row.result_json, row.status, row.updated_at = schema_json, source_hash, json.dumps(result), status, now
            session.add(AuditEventRow(id=str(uuid4()), entity_type="destination_proposal", entity_id=row.id,
                action="extracted", details_json=json.dumps({"database_id": target, "status": status,
                                                               "extraction_version": DESTINATION_EXTRACTION_VERSION}), created_at=now))
            session.commit()
            proposal_payload = payload(row, session.get(SupplierDatabaseRow, target), session.get(IntakeItemRow, intake_id))
            results.append(proposal_payload)
    proposal_statuses = {item.get("status") for item in results}
    if "failed" in proposal_statuses:
        gmail_status = "extraction_failed"
    elif "needs_information" in proposal_statuses:
        gmail_status = "needs_information"
    elif "ready_for_approval" in proposal_statuses:
        gmail_status = "ready_for_approval"
    elif "approved" in proposal_statuses:
        gmail_status = "approved"
    elif "imported" in proposal_statuses:
        gmail_status = "imported"
    elif proposal_statuses == {"rejected"}:
        gmail_status = "rejected"
    elif results:
        gmail_status = "needs_review"
    else:
        gmail_status = "no_destination_match"
    with get_session(settings) as session:
        intake_row = session.get(IntakeItemRow, intake_id)
        if intake_row:
            intake_row.processing_status = "processed" if results else "needs_review"
            if intake_row.gmail_message_id:
                from ..db.models import GmailMessageRow
                message = session.get(GmailMessageRow, intake_row.gmail_message_id)
                if message:
                    message.processing_status = gmail_status
        session.commit()
    # Generate or refresh drafts for new and previously saved proposals. This also
    # repairs a past run where extraction succeeded but draft persistence did not.
    for proposal_payload in results:
        try:
            from .supplier_workflow import generate_proposal_clarification_drafts
            proposal_payload["drafts"] = generate_proposal_clarification_drafts(
                settings, proposal_payload["id"], proposal_payload)
        except Exception as exc:  # drafts are local and must not stop quotation processing
            logger.warning("clarification drafts were not prepared for proposal %s: %s",
                           proposal_payload.get("id"), type(exc).__name__)
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
        if intake.gmail_message_id:
            from ..db.models import GmailMessageRow
            message = session.get(GmailMessageRow, intake.gmail_message_id)
            if message:
                message.processing_status = {
                    "approve": "approved", "reject": "rejected", "import": "imported",
                }[operation]
        session.add(AuditEventRow(id=str(uuid4()), entity_type="destination_proposal", entity_id=row.id,
            action=operation, details_json=json.dumps({"database_id": row.database_id}), created_at=row.updated_at))
        session.commit()
        result_payload = payload(row, database, intake)
        imported_database_id = database.id if operation == "import" else None
    if imported_database_id:
        from .supplier_master import sync_supplier_master
        sync_supplier_master(settings, imported_database_id)
    return result_payload

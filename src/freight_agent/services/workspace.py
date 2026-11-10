"""Canonical quotation workspace shared by web and CLI adapters."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from ..config import Settings
from typing import Any
from ..domain.models import Quotation, Carrier
from ..db.repositories import Repos
from ..agents.model import make_agent
from ..agents.prompts import wrap_untrusted
from ..db.models import (ClarificationTurnRow, DestinationProposalRow, SourceDocumentRow,
                         SupplierDatabaseRow, SupplierEmailDraftRow,
                         SupplierQuotationActionRow, GmailMessageRow)
from ..domain.enums import ReviewStatus, ValidationStatus, GapStatus, ComparisonStatus
from ..domain.exceptions import ValidationFailureError, NotFoundError
from ..domain.models import AuditEvent, QuoteExtraction, QuotationDetails, FieldEvidence, IntakeItem
from .common import with_session
from .validation import MATERIAL_FIELDS, snippet_grounded

REQUIRED = ("carrier_name", "origin", "destination", "base_price", "currency", "pricing_unit", "valid_until")


def value_of(quote: Quotation, field: str, carrier: Carrier | None = None) -> Any:
    if field == "carrier_name":
        return carrier.name if carrier else None
    if field in QuotationDetails.model_fields:
        return getattr(quote.details, field)
    return getattr(quote, field, None)


def readiness(repos: Repos, quote: Quotation) -> tuple[list[str], list[str], list[str]]:
    carrier = repos.get_carrier(str(quote.carrier_id)) if quote.carrier_id else None
    evidence = repos.evidence_for_quotation(str(quote.id))
    waived = {g.field_name for g in repos.list_gaps(str(quote.shipment_id))
              if g.quotation_id == quote.id and g.status == GapStatus.waived}
    missing = [f for f in REQUIRED if value_of(quote, f, carrier) is None and f not in waived]
    conflicts = sorted({e.field_name for e in evidence if e.validation_status == ValidationStatus.needs_review
                        and e.field_name not in waived})
    for field in REQUIRED:
        if value_of(quote, field, carrier) is not None and field not in waived and not any(
                e.field_name == field and e.validation_status == ValidationStatus.accepted for e in evidence):
            conflicts.append(field)
    source_errors = repos.session.scalars(select(SourceDocumentRow.parse_error).where(
        SourceDocumentRow.intake_item_id == str(quote.intake_item_id), SourceDocumentRow.parse_error.is_not(None))).all()
    gaps = [g.description for g in repos.list_gaps(str(quote.shipment_id))
            if g.quotation_id == quote.id and g.status == GapStatus.open and g.severity == "critical"]
    shipment = repos.get_shipment(str(quote.shipment_id))
    if shipment.status == "needs_review":
        gaps.append("Confirm the proposed shipment route before approval")
    if quote.valid_until and quote.valid_until < date.today() and "valid_until" not in waived:
        gaps.append("Quotation validity date has expired")
    return missing, sorted(set(conflicts)), gaps + ["A source attachment could not be parsed" for _ in source_errors]


def quotation_stage(repos: Repos, quote: Quotation, missing: list[str], conflicts: list[str], gaps: list[str]) -> str:
    if quote.disposition or quote.review_status == ReviewStatus.rejected:
        return quote.disposition.capitalize() if quote.disposition else "Rejected"
    if quote.review_status == ReviewStatus.imported:
        return "Imported"
    if quote.review_status == ReviewStatus.approved:
        return "Approved"
    pending_turns = repos.session.scalars(select(ClarificationTurnRow.id).where(
        ClarificationTurnRow.quotation_id == str(quote.id), ClarificationTurnRow.quotation_version == quote.version,
        ClarificationTurnRow.reply_text.is_(None))).all()
    if pending_turns:
        transport = repos.session.scalar(select(SupplierEmailDraftRow).where(SupplierEmailDraftRow.clarification_turn_id.in_(pending_turns)).order_by(SupplierEmailDraftRow.updated_at.desc()))
        if transport:
            if transport.status == "sent": return "Waiting for supplier"
            if transport.status == "sending": return "Check email delivery"
            if transport.status == "approved": return "Draft ready to send"
        from ..db.models import ClarificationDraftRow
        if repos.session.scalar(select(ClarificationDraftRow.id).where(ClarificationDraftRow.id.in_(pending_turns), ClarificationDraftRow.status == "draft")):
            return "Clarification draft"
    return "Resolve conflicts" if conflicts else "Missing information" if missing or gaps else "Ready for review"


def list_quotes(settings: Settings, archived: bool = False, include_intake: bool = False) -> list[dict[str, Any]]:
    with with_session(settings) as repos:
        result = []
        for quote in repos.list_quotations():
            if bool(quote.disposition or quote.review_status == ReviewStatus.rejected) != archived:
                continue
            carrier = repos.get_carrier(str(quote.carrier_id)) if quote.carrier_id else None
            missing, conflicts, gaps = readiness(repos, quote)
            stage = quotation_stage(repos, quote, missing, conflicts, gaps)
            result.append({**quote.model_dump(mode="json"), "supplier": carrier.name if carrier else None,
                           "stage": stage, "missing": missing, "conflicts": conflicts,
                           "filled": len(REQUIRED) - len(missing), "required": len(REQUIRED),
                           "can_approve": quote.review_status == ReviewStatus.extracted and not (missing or conflicts or gaps or quote.disposition),
                           "can_import": quote.review_status == ReviewStatus.approved and not quote.disposition})
        if include_intake and not archived:
            all_quotes = repos.list_quotations()
            consumed = {str(q.intake_item_id) for q in all_quotes}
            proposal_rows = repos.session.execute(
                select(DestinationProposalRow, SupplierDatabaseRow).join(
                    SupplierDatabaseRow,
                    DestinationProposalRow.database_id == SupplierDatabaseRow.id,
                ).order_by(DestinationProposalRow.created_at.desc())
            ).all()
            proposal_by_intake = {}
            for proposal_row, database_row in proposal_rows:
                proposal_by_intake.setdefault(str(proposal_row.intake_id), (proposal_row, database_row))
            for item in repos.list_intakes(None, None):
                if (str(item.id) in consumed or item.source_type == "imported_reply"
                        or item.classification_status != __import__("freight_agent.domain.enums", fromlist=["ClassificationStatus"]).ClassificationStatus.relevant):
                    continue
                proposal_pair = proposal_by_intake.get(str(item.id))
                proposal = database = result_data = None
                if proposal_pair:
                    proposal, database = proposal_pair
                    result_data = json.loads(proposal.result_json or "{}")
                    proposal_stages = {
                        "ready_for_approval": ("Ready for review", "extracted"),
                        "approved": ("Approved", "approved"),
                        "imported": ("Imported", "imported"),
                        "needs_information": ("Missing information", "extracted"),
                        "rejected": ("Rejected", "rejected"),
                        "failed": ("Reading failed", None),
                    }
                    stage, review_status = proposal_stages.get(proposal.status, ("Needs review", None))
                else:
                    stage = ("Reading" if item.processing_status == "processing" else
                             "Reading failed" if item.processing_status == "failed" else
                             "Confirm document type" if item.classification_status == "needs_review" else
                             "Needs review" if item.processing_status == "needs_review" else
                             "Analysis complete" if item.processing_status == "processed" else "Received")
                    review_status = None
                values = (result_data or {}).get("values", {})
                schema = json.loads(proposal.schema_json) if proposal else []
                required_fields = [field for field in schema if field.get("required")]
                missing = (result_data or {}).get("missing", [])
                uncertain = (result_data or {}).get("uncertain", [])
                field_statuses = (result_data or {}).get("field_status", {})
                if proposal and proposal.status == "needs_information" and not missing and uncertain:
                    stage = "Resolve conflicts"
                location = lambda city, country: ", ".join(
                    part for part in (str(values.get(city) or "").strip(),
                                      str(values.get(country) or "").strip()) if part)
                can_approve = bool(proposal and proposal.status == "ready_for_approval")
                can_import = bool(proposal and proposal.status == "approved")
                result.append({"id": str(item.id), "record_type": "intake", "intake_item_id": str(item.id),
                    "proposal_id": proposal.id if proposal else None,
                    "supplier": values.get("Supplier_Name"),
                    "quote_reference": values.get("Quotation_Reference") or values.get("Quotation_ID") or item.subject or item.original_filename,
                    "origin": location("Origin_City", "Origin_Country") or values.get("Origin_Port"),
                    "destination": location("Destination_City", "Destination_Country") or values.get("Destination_Port"),
                    "currency": values.get("Currency"), "base_price": values.get("Quoted_Price"),
                    "created_at": item.created_at.isoformat(), "review_status": review_status,
                    "stage": stage, "missing": missing, "conflicts": uncertain,
                    "filled": sum(1 for field in required_fields if values.get(field["name"]) not in (None, "")),
                    "required": len(required_fields) if proposal else None,
                    "can_approve": can_approve, "can_import": can_import,
                    "details": {"pricing_unit": values.get("Pricing_Basis")}})
        return result


def detail(settings: Settings, quote_id: str) -> dict[str, Any]:
    with with_session(settings) as repos:
        quote = repos.get_quotation(quote_id)
        quote_id = str(quote.id)
        carrier = repos.get_carrier(str(quote.carrier_id)) if quote.carrier_id else None
        item = repos.get_intake(str(quote.intake_item_id))
        evidence = repos.evidence_for_quotation(quote_id)
        source_ids = {str(quote.intake_item_id)} | {str(r.intake_item_id) for r in repos.list_replies(str(quote.shipment_id)) if str(r.quotation_id) == quote_id}
        sources = repos.session.scalars(select(SourceDocumentRow).where(SourceDocumentRow.intake_item_id.in_(source_ids))).all()
        source_data = [{"id": s.id, "filename": s.filename, "kind": s.source_kind, "text": s.text,
                        "location": s.location, "error": s.parse_error} for s in sources]
        if not source_data:
            source_data = [{"id": str(item.id), "filename": item.original_filename, "kind": "document", "text": item.raw_text}]
        fields = []
        not_applicable = set(quote.details.not_applicable_fields)
        for field in dict.fromkeys(REQUIRED + tuple(MATERIAL_FIELDS)):
            value = value_of(quote, field, carrier)
            supporting = [e.model_dump(mode="json") for e in evidence if e.field_name == field]
            required = field in REQUIRED
            status = ("Not applicable" if field in not_applicable else
                      "Needs review" if any(e["validation_status"] == "needs_review" for e in supporting) else
                      "Found" if value is not None and any(e["validation_status"] == "accepted" for e in supporting) else
                      "Source not verified" if value is not None else "Missing" if required else "Not stated")
            fields.append({"field": field, "value": str(value) if value is not None else None,
                           "required": required, "not_applicable": field in not_applicable,
                           "status": status, "evidence": supporting})
        missing, conflicts, gaps = readiness(repos, quote)
        from .supplier_workflow import action_for_intake
        from .supplier_databases import list_supplier_databases
        destination = action_for_intake(settings, str(quote.intake_item_id))
        destinations = list_supplier_databases(settings)
        turns = repos.session.scalars(select(ClarificationTurnRow).where(ClarificationTurnRow.quotation_id == quote_id).order_by(ClarificationTurnRow.created_at)).all()
        return {"quotation": quote.model_dump(mode="json"), "supplier": carrier.name if carrier else None,
                "stage": quotation_stage(repos, quote, missing, conflicts, gaps),
                "fields": fields, "sources": source_data, "destination": destination, "destinations": destinations, "blocking_reasons": gaps, "missing": missing, "conflicts": conflicts,
                "gaps": [g.model_dump(mode="json") for g in repos.list_gaps(str(quote.shipment_id)) if str(g.quotation_id) == quote_id],
                "history": [a.model_dump(mode="json") for a in repos.audit_for(quote_id)],
                "versions": [v.model_dump(mode="json") for v in repos.versions_for_quotation(quote_id)],
                "local_drafts": [d.model_dump(mode="json") for d in repos.list_drafts(str(quote.shipment_id)) if d.quotation_id == quote.id],
                "correspondence": [{"id": t.id, "questions": json.loads(t.questions_json), "reply": t.reply_text,
                                    "result": json.loads(t.result_json) if t.result_json else None} for t in turns],
                "can_approve": quote.review_status == ReviewStatus.extracted and not (missing or conflicts or gaps or quote.disposition),
                "can_clarify": bool(missing or conflicts or any(g.quotation_id == quote.id and g.status == GapStatus.open and g.severity != "informational" for g in repos.list_gaps(str(quote.shipment_id)))),
                "sender": item.sender, "subject": item.subject,
                "shipment": repos.get_shipment(str(quote.shipment_id)).model_dump(mode="json")}


def archive(settings: Settings, quote_id: str, disposition: str, reason: str, reason_code: str = "other") -> dict[str, Any]:
    if reason_code not in {"price", "terms", "withdrawn", "expired", "duplicate", "closed", "other"}:
        raise ValidationFailureError("Choose a valid archive reason")
    if disposition not in {"rejected", "invalid", "archived"} or not reason.strip():
        raise ValidationFailureError("Choose rejected, invalid or archived and provide a reason")
    with with_session(settings) as repos, repos.atomic():
        quote = repos.get_quotation(quote_id)
        quote_id = str(quote.id)
        quote.disposition, quote.disposition_reason = disposition, reason.strip()
        quote.archived_at = datetime.now(timezone.utc)
        repos.update_quotation(quote)
        repos.add_audit(AuditEvent(entity_type="quotation", entity_id=quote.id, action="archived",
                                  details_json=json.dumps({"disposition": disposition, "reason": reason, "reason_code": reason_code})))
    return {"ok": True}


def approve(settings: Settings, quote_id: str, notes: str | None = None) -> dict[str, Any]:
    from .review import _approve_review
    with with_session(settings) as repos:
        quote = repos.get_quotation(quote_id)
        quote_id = str(quote.id)
        missing, conflicts, gaps = readiness(repos, quote)
        if quote.disposition or missing or conflicts or gaps:
            raise ValidationFailureError("Resolve or waive missing information and conflicts before approval")
    from .supplier_workflow import approve_action
    with with_session(settings) as repos:
        action = repos.session.scalar(select(SupplierQuotationActionRow).where(SupplierQuotationActionRow.intake_id == str(quote.intake_item_id)))
        action_id = action.id if action and action.database_id else None
    if action_id:
        approve_action(settings, action_id)
    return _approve_review(settings, quote_id, notes).model_dump(mode="json")


def start_questions(settings: Settings, quote_id: str) -> dict[str, Any]:
    with with_session(settings) as repos, repos.atomic():
        quote = repos.get_quotation(quote_id)
        quote_id = str(quote.id)
        if quote.disposition or quote.review_status == ReviewStatus.rejected:
            raise ValidationFailureError("Archived quotations are read-only")
        missing, conflicts, _ = readiness(repos, quote)
        from .gap_analysis import askable_question, canonical_field
        fields = list(dict.fromkeys([canonical_field(field) for field in missing + conflicts] + [canonical_field(g.field_name) for g in repos.list_gaps(str(quote.shipment_id))
                           if g.quotation_id == quote.id and g.status == GapStatus.open and g.severity != "informational"
                           and g.field_name != "total_known_cost"]))
        existing = repos.session.scalar(select(ClarificationTurnRow).where(
            ClarificationTurnRow.quotation_id == quote_id, ClarificationTurnRow.quotation_version == quote.version,
            ClarificationTurnRow.reply_text.is_(None)).order_by(ClarificationTurnRow.created_at.desc()))
        if existing and {q["field"] for q in json.loads(existing.questions_json)} == set(fields):
            return {"id": existing.id, "questions": json.loads(existing.questions_json), "quotation_version": quote.version}
        questions = [{"id": str(uuid4()), "field": f, "text": askable_question(f)} for f in fields]
        if not questions:
            raise ValidationFailureError("No unresolved questions remain")
        row = ClarificationTurnRow(id=str(uuid4()), quotation_id=quote_id, quotation_version=quote.version,
                                  questions_json=json.dumps(questions), created_at=datetime.now(timezone.utc).isoformat())
        repos.session.add(row)
        from ..domain.models import QuestionSet, ClarificationDraft, ClarificationQuestion
        from .supplier_workflow import _template_text
        from email.utils import parseaddr
        item = repos.get_intake(str(quote.intake_item_id))
        carrier = repos.get_carrier(str(quote.carrier_id)) if quote.carrier_id else None
        question_set = QuestionSet(id=row.id, shipment_id=quote.shipment_id, quotation_id=quote.id,
            carrier_id=quote.carrier_id, quotation_version=quote.version,
            questions=[ClarificationQuestion(field_name=q["field"], why_it_matters="Resolve missing or ambiguous quotation information",
                question_text=q["text"], priority="important", source_context="not found") for q in questions])
        repos.add_question_set(question_set)
        subject, body = _template_text(carrier.name if carrier else "", [{"question": q["text"]} for q in questions])
        repos.add_draft(ClarificationDraft(id=row.id, shipment_id=quote.shipment_id, quotation_id=quote.id,
            carrier_id=quote.carrier_id, question_set_id=question_set.id, quotation_version=quote.version,
            recipient=parseaddr(item.sender or "")[1] or None, subject=subject, body=body))
        return {"id": row.id, "questions": questions, "quotation_version": quote.version}


def answer(settings: Settings, quote_id: str, turn_id: str, text: str) -> dict[str, Any]:
    """Interpret against one immutable question set; numbers and sources stay explicit."""
    from .replies import _merge_reply
    if len(text) > settings.max_text_chars:
        raise ValidationFailureError("Response exceeds the configured text limit; upload it as a source document")
    if not text.strip():
        raise ValidationFailureError("Enter the supplier response")
    with with_session(settings) as repos:
        quote = repos.get_quotation(quote_id)
        quote_id = str(quote.id)
        if quote.disposition or quote.review_status == ReviewStatus.rejected:
            raise ValidationFailureError("Archived quotations are read-only")
        turn = repos.session.get(ClarificationTurnRow, turn_id)
        if turn is None or turn.quotation_id != quote_id:
            raise NotFoundError("Clarification questions not found")
        if turn.reply_text is not None:
            if turn.reply_text.strip() != text.strip():
                raise ValidationFailureError("This question set already has a recorded answer; use the current questions for a new response")
            return json.loads(turn.result_json)
        if turn.quotation_version != quote.version:
            raise ValidationFailureError("Quotation changed; create current questions before interpreting this response")
        questions = json.loads(turn.questions_json)
        transport = repos.session.scalar(select(SupplierEmailDraftRow).where(SupplierEmailDraftRow.clarification_turn_id == turn_id).order_by(SupplierEmailDraftRow.updated_at.desc()))
        sent_body = transport.immutable_approved_body if transport and transport.status in {"sent", "approved"} else None
        old = repos.evidence_for_quotation(quote_id)
        carrier = repos.get_carrier(str(quote.carrier_id)) if quote.carrier_id else None
    if text.strip().lower().rstrip(".! ") in {"confirm", "confirmed", "yes", "ok"}:
        if len(questions) == 1 and questions[0].get("confirmation"):
            result = {"status": "confirmed", "message": "Confirmation recorded. This does not authorize sending or importing."}
            with with_session(settings) as repos, repos.atomic():
                current = repos.session.get(ClarificationTurnRow, turn_id)
                current.reply_text, current.result_json = text, json.dumps(result)
                repos.add_audit(AuditEvent(entity_type="quotation", entity_id=quote.id, action="human_confirmation",
                    details_json=json.dumps({"turn_id": turn_id, "proposition": questions[0]["text"], "answer": text})))
            return result
        # A missing-field question does not establish a proposition to confirm.
        result = {"status": "needs_clarification", "message": "Please specify the value being confirmed; no email was sent and no quotation value changed."}
        with with_session(settings) as repos, repos.atomic():
            repos.add_audit(AuditEvent(entity_type="quotation", entity_id=quote.id, action="confirmation_needs_context",
                                      details_json=json.dumps({"turn_id": turn_id, "answer": text})))
        return result
    parts = [re.sub(r"^\s*\d+[.)]\s*", "", p).strip() for p in re.split(r"\n|,\s*", text) if p.strip()]
    values, evidence = {}, []
    if len(parts) == len(questions) and all(q.get("order_verified", True) for q in questions):
        for q, part in zip(questions, parts):
            field = q["field"]
            if field not in QuoteExtraction.model_fields:
                continue
            if field in {"base_price", "weight_kg", "volume_m3", "transit_days_min", "transit_days_max"}:
                if not re.fullmatch(r"\d+(?:\.\d+)?", part):
                    continue
                value = part
            elif field in {"valid_until", "valid_from"} and re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
                value = part
            elif field in {"insurance_included", "customs_included"} and part.casefold() in {"yes", "no", "true", "false"}:
                value = part.casefold() in {"yes", "true"}
            elif field in {"carrier_name", "supplier_id", "currency", "pricing_unit", "origin", "destination", "contact_email", "contact_phone", "transport_type", "fuel_type", "additional_conditions", "delivery_scope"}:
                value = part
            else:
                continue
            values[field] = value
            evidence.append({"field_name": field, "snippet": part})
    if len(values) != len(questions):
        agent = make_agent(settings, QuoteExtraction,
            "Interpret answers only in relation to the supplied ordered questions. Return explicit facts with exact reply snippets. "
            "Context maps answers to fields but is not evidence of new values. Never guess currency or units. Ambiguous answers stay null.", "contextual_reply")
        try:
            extraction = agent.run_sync(wrap_untrusted("reply.txt", text, "supplier_reply") + "\n" +
                wrap_untrusted("questions.json", json.dumps({"questions": questions, "exact_sent_email": sent_body}), "question_context") + "\n" +
                wrap_untrusted("quotation_context.json", quote.model_dump_json(), "existing_quotation")).output
        except Exception as exc:
            from ..domain.exceptions import ModelUnavailableError
            raise ModelUnavailableError("The local model could not interpret this response. Keep the response and retry, or use numbered answers.") from exc
    else:
        from pydantic import ValidationError
        try:
            extraction = QuoteExtraction(**values, field_evidence=evidence)
        except ValidationError as exc:
            raise ValidationFailureError("An answer could not be validated; check dates, amounts and units before retrying") from exc
    allowed = {q["field"] for q in questions}
    if "transit_days" in allowed:
        allowed.update({"transit_days_min", "transit_days_max"})
    extraction = QuoteExtraction.model_validate({**{f: getattr(extraction, f) for f in allowed if f in QuoteExtraction.model_fields},
        "field_evidence": [ev for ev in extraction.field_evidence if ev.field_name in allowed]})
    item = IntakeItem(original_filename=f"supplier-response-{turn_id[:8]}.txt", stored_path="", content_sha256=__import__('hashlib').sha256(text.encode()).hexdigest(), raw_text=text, shipment_id=quote.shipment_id, source_type="imported_reply", classification_status="relevant", processing_status="processed")
    merged, new_evidence, new_gaps = _merge_reply(quote, extraction, item, old, carrier.name if carrier else None)
    from .gap_analysis import detect_gaps_deterministic, comparison_status_after_gaps
    from ..domain.models import Gap
    with with_session(settings) as repos:
        shipment = repos.get_shipment(str(quote.shipment_id))
        waived = {g.field_name for g in repos.list_gaps(str(quote.shipment_id)) if g.quotation_id == quote.id and g.status == GapStatus.waived}
    derived = [gap for gap in detect_gaps_deterministic(shipment, merged) if gap.field_name not in waived]
    new_gaps += [Gap(shipment_id=quote.shipment_id, quotation_id=quote.id, **gap.model_dump()) for gap in derived]
    merged.comparison_status = comparison_status_after_gaps(merged, derived)
    result = {"status": "updated", "version": merged.version, "intake_id": str(item.id), "message": "Response recorded. Review any conflicting values before approval."}
    with with_session(settings) as repos, repos.atomic():
        current = repos.get_quotation(quote_id)
        if current.version != quote.version:
            raise ValidationFailureError("Quotation changed while interpreting the reply")
        repos.add_intake(item)
        from ..domain.models import SupplierReply
        repos.add_reply(SupplierReply(shipment_id=quote.shipment_id, quotation_id=quote.id,
                        intake_item_id=item.id, carrier_id=quote.carrier_id, association_method="manual"))
        repos.session.add(SourceDocumentRow(id=str(uuid4()), intake_item_id=str(item.id), filename=item.original_filename,
                          source_kind="supplier_response", text=text))
        if not merged.carrier_id and extraction.carrier_name and any(e.field_name == "carrier_name" and e.validation_status == ValidationStatus.accepted for e in new_evidence):
            merged.carrier_id = repos.get_or_create_carrier(extraction.carrier_name).id
        repos.record_quotation_version(quote, old)
        repos.update_quotation(merged)
        repos.delete_evidence_for_quotation(quote_id)
        repos.add_evidence(new_evidence)
        existing_gaps = [gap for gap in repos.list_gaps(str(quote.shipment_id)) if gap.quotation_id == quote.id]
        active = [gap for gap in new_gaps if gap.field_name not in waived]
        new_keys = {(gap.field_name, gap.gap_type, gap.description) for gap in active}
        old_keys = {(gap.field_name, gap.gap_type, gap.description) for gap in existing_gaps if gap.status == GapStatus.open}
        for gap in existing_gaps:
            if gap.status == GapStatus.open and (gap.field_name, gap.gap_type, gap.description) not in new_keys:
                gap.status = GapStatus.resolved
                gap.resolved_at = merged.updated_at
                repos.update_gap(gap)
        repos.add_gaps([gap for gap in active if (gap.field_name, gap.gap_type, gap.description) not in old_keys])
        repos.record_quotation_version(merged, new_evidence, item.id)
        turn = repos.session.get(ClarificationTurnRow, turn_id)
        turn.reply_text, turn.result_json = text, json.dumps(result)
        from ..db.models import ClarificationDraftRow
        local_draft = repos.session.get(ClarificationDraftRow, turn_id)
        if local_draft:
            local_draft.status = "cancelled"
        repos.add_audit(AuditEvent(entity_type="quotation", entity_id=quote.id, action="contextual_reply_applied",
                                  details_json=json.dumps({"turn_id": turn_id, "version": merged.version})))
    return result


def resolve(settings: Settings, quote_id: str, evidence_id: str, use_candidate: bool, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValidationFailureError("Explain the conflict decision")
    with with_session(settings) as repos, repos.atomic():
        quote = repos.get_quotation(quote_id)
        quote_id = str(quote.id)
        if quote.disposition or quote.review_status == ReviewStatus.rejected:
            raise ValidationFailureError("Archived quotations are read-only")
        ev = repos.get_evidence(evidence_id)
        if ev.quotation_id != quote.id or ev.validation_status != ValidationStatus.needs_review:
            raise ValidationFailureError("Choose an unresolved evidence item for this quotation")
        old = repos.evidence_for_quotation(quote_id)
        repos.record_quotation_version(quote, old)
        if use_candidate:
            field = ev.field_name
            from ..domain.models import Charge
            candidate_value = ev.value_as_extracted
            if field.startswith("charges:"):
                charge = Charge.model_validate_json(candidate_value)
                candidate = QuoteExtraction(charges=[charge])
            elif field in {"included_services", "excluded_services"}:
                candidate = QuoteExtraction.model_validate({field: json.loads(candidate_value), "field_evidence": [{"field_name": field, "snippet": ev.snippet}]})
            elif field in QuoteExtraction.model_fields:
                candidate = QuoteExtraction.model_validate({field: candidate_value, "field_evidence": [{"field_name": field, "snippet": ev.snippet}]})
            else:
                raise ValidationFailureError("This conflict requires structured reprocessing")
            from .validation import validate_extraction, compute_total_known_cost
            source_ids = {str(quote.intake_item_id)} | {str(r.intake_item_id) for r in repos.list_replies(str(quote.shipment_id)) if r.quotation_id == quote.id}
            sources = repos.session.scalars(select(SourceDocumentRow).where(SourceDocumentRow.filename == ev.source_filename,
                SourceDocumentRow.intake_item_id.in_(source_ids))).all()
            source_items = [repos.get_intake(source_id) for source_id in source_ids]
            raw = "\n".join(s.text for s in sources) + "\n" + "\n".join(i.raw_text for i in source_items if i.original_filename == ev.source_filename)
            cleaned, _, _, _ = validate_extraction(candidate, raw, ev.source_filename)
            if field in cleaned.fields_requiring_review or not snippet_grounded(ev.snippet, raw):
                raise ValidationFailureError("Candidate does not have valid source evidence")
            if field.startswith("charges:"):
                charge = cleaned.charges[0]
                quote.known_additional_charges = [c for c in quote.known_additional_charges if c.label.casefold() != charge.label.casefold()] + [charge]
            elif field in QuotationDetails.model_fields:
                setattr(quote.details, field, getattr(cleaned, field))
            elif field == "carrier_name":
                quote.carrier_id = repos.get_or_create_carrier(cleaned.carrier_name).id
            else:
                setattr(quote, field, getattr(cleaned, field))
                if field in {"included_services", "excluded_services"}:
                    opposite = "excluded_services" if field == "included_services" else "included_services"
                    selected = {v.casefold() for v in getattr(cleaned, field)}
                    setattr(quote, opposite, [v for v in getattr(quote, opposite) if v.casefold() not in selected])
            for previous in old:
                if previous.field_name == field and previous.id != ev.id:
                    previous.validation_status = ValidationStatus.rejected
                    repos.update_evidence(previous)
            quote.total_known_cost, _ = compute_total_known_cost(quote.base_price, quote.currency, quote.known_additional_charges, quote.details.pricing_unit)
            ev.validation_status = ValidationStatus.accepted
        else:
            ev.validation_status = ValidationStatus.rejected
        repos.update_evidence(ev)
        for gap in repos.list_gaps(str(quote.shipment_id)):
            if gap.quotation_id == quote.id and gap.field_name == ev.field_name and gap.status == GapStatus.open:
                gap.status = GapStatus.resolved
                repos.update_gap(gap)
        quote.version += 1
        quote.review_status = ReviewStatus.extracted
        quote.reviewed_at = quote.imported_at = None
        repos.update_quotation(quote)
        repos.record_quotation_version(quote, repos.evidence_for_quotation(quote_id))
        repos.add_audit(AuditEvent(entity_type="quotation", entity_id=quote.id, action="conflict_resolved",
                                  details_json=json.dumps({"evidence_id": evidence_id, "use_candidate": use_candidate, "reason": reason})))
    return {"ok": True}


def compare(settings: Settings, shipment_id: str, question: str = "Which quotation is cheapest?", pricing_unit: str | None = None) -> dict[str, Any]:
    from .comparison import build_comparison
    report = build_comparison(settings, shipment_id, pricing_unit)
    rows = [row.model_dump(mode="json") for row in report.rows]
    eligible = [r for r in rows if not r["exclusions"]]
    if re.search(r"risk|reliab", question, re.I):
        intent = "risk"
    elif re.search(r"balance|best|advantage|recommend", question, re.I):
        intent = "balance"
    elif re.search(r"which suppliers|list|available|transportation from", question, re.I):
        intent = "inventory"
    elif re.search(r"fast|quick|speed|shortest", question, re.I):
        intent = "fastest"
    elif re.search(r"cheap|price|cost|lowest", question, re.I):
        intent = "cheapest"
    else:
        try:
            from ..agents.comparison_intent import interpret
            parsed = interpret(settings, question)
            intent = parsed.criterion
            if parsed.origin and parsed.origin.casefold() not in report.route.casefold() or parsed.destination and parsed.destination.casefold() not in report.route.casefold():
                eligible = []
        except Exception:
            intent = "unclear"
    requested_lane = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)[?.]?$", question.strip(), re.I)
    lane_mismatch = bool(requested_lane and any(place.strip().casefold() not in report.route.casefold() for place in requested_lane.groups()))
    if lane_mismatch:
        eligible = []
    named = [r["quotation_id"] for r in rows if any(value and str(value).casefold() in question.casefold() for value in (r["carrier_name"], r["quote_reference"]))]
    if len(named) >= 2:
        eligible = [r for r in eligible if r["quotation_id"] in named]
    explanation = "No quotations are currently comparable. Review the reasons beside each row."
    selected = []
    if lane_mismatch:
        explanation = "The requested route differs from the selected lane. Choose the matching shipment before comparing."
    elif intent == "risk":
        explanation = "Delivery risk cannot be ranked from price and transit time alone. Verified reliability data and a risk policy are required."
    elif intent == "unclear":
        explanation = "Please specify price, transit time, or the suppliers you want to compare."
    elif intent == "inventory":
        selected = [r["quotation_id"] for r in rows]
        explanation = f"{len(rows)} quotation(s) recorded for {report.route}. Eligibility and missing information are shown for each."
    elif intent == "balance":
        candidates = [r for r in eligible if r["transit_days"] is not None]
        selected = [r["quotation_id"] for r in candidates if not any(
            Decimal(o["converted_subtotal"]) <= Decimal(r["converted_subtotal"]) and o["transit_days"] <= r["transit_days"]
            and (Decimal(o["converted_subtotal"]) < Decimal(r["converted_subtotal"]) or o["transit_days"] < r["transit_days"])
            for o in candidates)]
        explanation = ("Highlighted options are not beaten on both price and transit time. " + "; ".join(
            f"{r['carrier_name'] or r['quote_reference'] or 'Supplier'}: {r['converted_subtotal']} {report.target_currency} per {r['pricing_unit']}, up to {r['transit_days']} days"
            for r in candidates if r['quotation_id'] in selected) + ". Choose the trade-off that fits your shipment; no risk score is assumed."
            if selected else "No eligible quotations have both a comparable price and a quoted transit bound for this trade-off.")
    elif eligible:
        key = "transit_days" if intent == "fastest" else "converted_subtotal"
        available = [r for r in eligible if r[key] is not None]
        if available:
            best = min(Decimal(str(r[key])) for r in available)
            selected = [r["quotation_id"] for r in available if Decimal(str(r[key])) == best]
            names = ", ".join(r["carrier_name"] or r["quote_reference"] or "Supplier" for r in available if r["quotation_id"] in selected)
            explanation = (f"{names} has the shortest quoted upper transit bound: {best} days." if intent == "fastest" else
                           f"{names} offers the lowest comparable total: {best} {report.target_currency} per {available[0]['pricing_unit']}.")
            next_values = sorted({Decimal(str(r[key])) for r in available if Decimal(str(r[key])) > best})
            if next_values and intent == "cheapest":
                explanation += f" The next eligible quotation is {next_values[0] - best} {report.target_currency} higher."
        elif intent == "fastest":
            explanation = "Eligible quotations do not state comparable upper transit bounds, so the fastest delivery cannot be established."
    return {"rows": rows, "selected_ids": selected, "highlight_fields": ["transit_time" if intent == "fastest" else "converted_subtotal"],
            "explanation": explanation, "route": report.route, "currency": report.target_currency, "intent": intent}


def prepare_draft(settings: Settings, quote_id: str, turn_id: str, recipient: str, subject: str, body: str) -> dict[str, Any]:
    from ..db.models import GmailAccountRow
    from .gmail_drafts import create_draft, approve_draft, save_draft
    save_local_draft(settings, quote_id, turn_id, recipient, subject, body)
    from email.utils import parseaddr
    with with_session(settings) as repos:
        quote = repos.get_quotation(quote_id)
        quote_id = str(quote.id)
        if quote.disposition or quote.review_status == ReviewStatus.rejected:
            raise ValidationFailureError("Archived quotations are read-only")
        item = repos.get_intake(str(quote.intake_item_id))
        turn = repos.session.get(ClarificationTurnRow, turn_id)
        if not turn or turn.quotation_id != quote_id or turn.quotation_version != quote.version:
            raise ValidationFailureError("Clarification questions are stale")
        account = repos.session.scalar(select(GmailAccountRow).where(GmailAccountRow.status == "connected"))
        if not account:
            raise ValidationFailureError("Connect Gmail in Settings before approving a draft for sending. You can still copy the draft.")
        sender = parseaddr(item.sender or "")[1]
        if sender and parseaddr(recipient)[1].lower() != sender.lower():
            raise ValidationFailureError("Recipient must match the verified quotation sender")
        existing = repos.session.scalar(select(SupplierEmailDraftRow).where(SupplierEmailDraftRow.clarification_turn_id == turn_id))
        existing_id = existing.id if existing else None
        if existing and existing.status in {"sending", "sent"}:
            raise ValidationFailureError("This clarification has already been sent or is awaiting delivery confirmation")
        account_id = account.id
    if existing_id:
        draft = save_draft(settings, existing_id, recipient=recipient, cc="", subject=subject, body=body)
    else:
        draft = create_draft(settings, recipient=recipient, cc="", subject=subject, body=body,
                             gmail_account_id=account_id, bound_recipient=sender or None)
    with with_session(settings) as repos, repos.atomic():
        saved = repos.session.get(SupplierEmailDraftRow, draft["id"])
        saved.quotation_id = quote_id
        saved.clarification_turn_id = turn_id
        turn = repos.session.get(ClarificationTurnRow, turn_id)
        questions = json.loads(turn.questions_json)
        order_verified = all(q["text"] in body for q in questions)
        if order_verified:
            questions.sort(key=lambda q: body.index(q["text"]))
        for question in questions:
            question["order_verified"] = order_verified
        turn.questions_json = json.dumps(questions)
        from ..db.models import IntakeItemRow
        intake_row = repos.session.get(IntakeItemRow, str(quote.intake_item_id))
        message = repos.session.get(GmailMessageRow, intake_row.gmail_message_id) if intake_row.gmail_message_id else None
        if message:
            saved.gmail_thread_id = message.gmail_thread_id
    return approve_draft(settings, draft["id"])


def import_quote(settings: Settings, quote_id: str) -> dict[str, Any]:
    """Journal destination progress so retries do not append duplicate records."""
    from ..db.models import DestinationImportRow
    from .review import _commit_reviewed
    from .supplier_workflow import import_action
    with with_session(settings) as repos, repos.atomic():
        quote = repos.get_quotation(quote_id)
        quote_id = str(quote.id)
        if quote.disposition or quote.review_status not in {ReviewStatus.approved, ReviewStatus.imported}:
            raise ValidationFailureError("Approve the review before importing; import only commits an approved extraction.")
        key = f"{quote.id}:{quote.version}"
        journal = repos.session.get(DestinationImportRow, key)
        if journal and journal.status == "complete":
            return {"already_imported": True, "quotation_id": quote_id, "version": quote.version, "message": "Quotation was already imported; nothing changed"}
        action = repos.session.scalar(select(SupplierQuotationActionRow).where(SupplierQuotationActionRow.intake_id == str(quote.intake_item_id)))
        action_id = action.id if action and action.database_id else None
        if action_id and action.status not in {"approved", "record_created"}:
            raise ValidationFailureError("Review and approve the supplier destination proposal before importing")
        if journal is None:
            repos.session.add(DestinationImportRow(id=key, quotation_id=quote_id, version=quote.version,
                status="pending", details_json=json.dumps({"supplier_action_id": action_id}), created_at=datetime.now(timezone.utc).isoformat()))
    if action_id:
        import_action(settings, action_id)
    result = _commit_reviewed(settings, quote_id)
    with with_session(settings) as repos, repos.atomic():
        repos.session.get(DestinationImportRow, key).status = "complete"
    return result


def request_confirmation(settings: Settings, quote_id: str) -> dict[str, Any]:
    """Record a human delivery report separately from actual email transport authorization."""
    with with_session(settings) as repos, repos.atomic():
        quote = repos.get_quotation(quote_id)
        quote_id = str(quote.id)
        if quote.disposition or quote.review_status == ReviewStatus.rejected:
            raise ValidationFailureError("Archived quotations are read-only")
        questions = [{"id": str(uuid4()), "field": "email_delivery_report", "confirmation": True,
                      "text": "Please confirm whether you sent the clarification outside this application."}]
        turn = ClarificationTurnRow(id=str(uuid4()), quotation_id=quote_id, quotation_version=quote.version,
            questions_json=json.dumps(questions), created_at=datetime.now(timezone.utc).isoformat())
        repos.session.add(turn)
        return {"id": turn.id, "questions": questions}


def waive_field(settings: Settings, quote_id: str, field: str, reason: str) -> dict[str, Any]:
    from ..domain.models import Gap
    from .comparison import waive_gap
    if field not in REQUIRED or not reason.strip():
        raise ValidationFailureError("Choose a required field and give a waiver reason")
    with with_session(settings) as repos, repos.atomic():
        quote = repos.get_quotation(quote_id)
        quote_id = str(quote.id)
        if quote.disposition or quote.review_status == ReviewStatus.rejected:
            raise ValidationFailureError("Archived quotations are read-only")
        existing = next((g for g in repos.list_gaps(str(quote.shipment_id)) if g.quotation_id == quote.id and g.field_name == field and g.status == GapStatus.open), None)
        if existing is None:
            existing = Gap(shipment_id=quote.shipment_id, quotation_id=quote.id, field_name=field,
                gap_type="missing", severity="critical", description=f"{field} missing or unverified")
            repos.add_gaps([existing])
    waive_gap(settings, str(existing.id), reason)
    return {"ok": True}


def save_local_draft(settings: Settings, quote_id: str, turn_id: str, recipient: str, subject: str, body: str) -> dict[str, Any]:
    from ..db.models import ClarificationDraftRow
    if not subject.strip() or not body.strip():
        raise ValidationFailureError("Enter a subject and draft content")
    with with_session(settings) as repos, repos.atomic():
        quote = repos.get_quotation(quote_id)
        turn = repos.session.get(ClarificationTurnRow, turn_id)
        draft = repos.session.get(ClarificationDraftRow, turn_id)
        if quote.disposition or not turn or not draft or turn.quotation_id != str(quote.id) or turn.quotation_version != quote.version or turn.reply_text is not None:
            raise ValidationFailureError("This draft is archived, answered or belongs to an older quotation version")
        draft.recipient, draft.subject, draft.body = recipient.strip() or None, subject.strip(), body
        draft.status = "draft"
        draft.approved_at = None
        draft.updated_at = datetime.now(timezone.utc).isoformat()
        transport = repos.session.scalar(select(SupplierEmailDraftRow).where(SupplierEmailDraftRow.clarification_turn_id == turn_id))
        if transport and transport.status in {"sent", "sending"}:
            raise ValidationFailureError("Sent or pending-delivery drafts cannot be edited")
        if transport:
            transport.status = "draft"
            transport.recipient, transport.subject, transport.body = recipient.strip(), subject.strip(), body
            transport.cc = ""
            transport.immutable_approved_body = transport.approved_subject = transport.approved_recipient = None
        repos.add_audit(AuditEvent(entity_type="quotation", entity_id=quote.id, action="clarification_draft_saved",
            details_json=json.dumps({"turn_id": turn_id})))
        return {"ok": True, "id": turn_id}

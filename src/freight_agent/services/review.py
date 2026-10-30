"""Review console data and actions shared by the local web page and the CLI.

Everything here is read-only aggregation plus thin dispatch onto the existing
workflow services, so every action keeps the same validation and audit trail as
the equivalent `freight ...` command.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..domain.enums import (ClassificationStatus, DraftStatus, GapStatus,
                            ProcessingStatus, ReviewStatus, ShipmentStatus,
                            ValidationStatus)
from ..domain.exceptions import NotFoundError, ValidationFailureError
from ..domain.models import AuditEvent, FieldEvidence, IntakeItem, Quotation
from .common import with_session
from .shipments import route_label

INTAKE_PREVIEW_CHARS = 700
ACTIONABLE_PROCESSING = {ProcessingStatus.pending, ProcessingStatus.needs_review, ProcessingStatus.failed}
OPEN_DRAFT_STATES = {DraftStatus.draft, DraftStatus.approved}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:INTAKE_PREVIEW_CHARS] + ("…" if len(flat) > INTAKE_PREVIEW_CHARS else "")


# Audit rows store machine actions; people read sentences. No ids, no JSON on the page.
_ACTIVITY_ACTIONS = {
    "created": "Shipment created",
    "auto_proposed": "New route proposed from a document",
    "created_from_intake": "Quote extracted from a document",
    "linked_to_shipment": "Document linked to its shipment",
    "review_approved": "Review approved — import still pending",
    "review_rejected": "Quotation rejected by the reviewer",
    "imported_to_database": "Approved quotation imported to the database",
    "rejected": "Document marked not relevant",
    "marked_irrelevant": "Document marked not relevant",
    "evidence_accepted": "Extracted value accepted",
    "evidence_rejected": "Extracted value flagged for review",
    "gap_waived": "Open item waived with a reason",
    "generated": "Clarification questions prepared",
    "superseded": "Questions replaced by a newer quote version",
    "approved": "Clarification draft approved",
    "exported": "Clarification draft exported to a local file",
    "cancelled_stale": "Outdated draft cancelled",
    "reply_imported": "Supplier reply imported",
    "reply_processed": "Supplier reply applied to the quote",
    "reprocessed_from_intake": "Quote re-extracted from the document",
    "reprocessed": "Document processed again",
    "forced": "Override recorded by the reviewer",
    "edited": "Record corrected by the reviewer",
}
_ACTIVITY_ENTITIES = {
    "intake_item": "document", "quotation": "quotation", "shipment": "shipment",
    "clarification_draft": "clarification draft", "question_set": "questions",
    "field_evidence": "evidence", "data_gap": "open item", "supplier_reply": "supplier reply",
}


def _activity_text(entity: str, action: str, subject: str | None = None) -> str:
    base = re.sub(r"_v(\d+)$", "", action)
    sentence = _ACTIVITY_ACTIONS.get(base)
    if sentence is None:
        label = _ACTIVITY_ENTITIES.get(entity, entity.replace("_", " "))
        sentence = f"{action.replace('_', ' ')} · {label}"
    version = re.search(r"_v(\d+)$", action)
    if version:
        sentence = f"{sentence} (version {version.group(1)})"
    return f"{sentence} — {subject}" if subject else sentence


def shipment_label(shipment) -> str:
    """What a person reads: the short route for agent-created shipments, the reference otherwise."""
    if shipment.reference.startswith("AUTO-"):
        return route_label(shipment.origin, shipment.destination)
    return shipment.reference


def build_review(settings: Settings) -> dict[str, Any]:
    from .comparison import build_comparison

    with with_session(settings) as repos:
        intakes = list(reversed(repos.list_intakes(None, None)))
        shipments = list(reversed(repos.list_shipments()))
        quotations = repos.list_quotations(None)
        by_shipment: dict[str, list] = {}
        for quote in quotations:
            by_shipment.setdefault(str(quote.shipment_id), []).append(quote)
        carriers = {c.id: c.name for c in repos.list_carriers()}

        # audit rows name an entity id; the page names the thing a person uploaded or approved
        names: dict[str, str] = {}
        for item in intakes:
            names[f"intake_item:{item.id}"] = item.original_filename
        for shipment in shipments:
            names[f"shipment:{shipment.id}"] = shipment_label(shipment)
        for quote in quotations:
            names[f"quotation:{quote.id}"] = (
                f"{carriers.get(quote.carrier_id) or quote.quote_reference or 'quote'} v{quote.version}")

        documents = []
        for item in intakes:
            if item.processing_status in ACTIONABLE_PROCESSING or item.classification_status in (
                    ClassificationStatus.pending, ClassificationStatus.needs_review):
                documents.append({
                    "id": str(item.id),
                    "file": item.original_filename,
                    "source": str(item.source_type),
                    "sender": item.sender,
                    "subject": item.subject,
                    "classification": str(item.classification_status),
                    "confidence": item.classification_confidence,
                    "processing": str(item.processing_status),
                    "reason": item.classification_reason,
                    "shipment_id": str(item.shipment_id) if item.shipment_id else None,
                    "created_at": _iso(item.created_at),
                    "preview": _preview(item.raw_text),
                })

        shipment_cards = []
        for shipment in shipments:
            if shipment.status == ShipmentStatus.closed:
                continue
            all_drafts = repos.list_drafts(str(shipment.id))
            shipment_gaps = repos.list_gaps(str(shipment.id))
            gaps = [g for g in shipment_gaps if g.status == GapStatus.open]
            for draft in all_drafts:
                names[f"clarification_draft:{draft.id}"] = (
                    names.get(f"quotation:{draft.quotation_id}") or "questions for a carrier")
            for gap in shipment_gaps:
                names[f"data_gap:{gap.id}"] = f"{gap.field_name} on {shipment_label(shipment)}"
            quotes = []
            for quote in sorted(by_shipment.get(str(shipment.id), []), key=lambda q: str(q.carrier_id or "")):
                evidence = [e for e in repos.evidence_for_quotation(str(quote.id))
                            if e.validation_status != ValidationStatus.accepted]
                quotes.append({
                    "id": str(quote.id),
                    "carrier": carriers.get(quote.carrier_id) or (quote.quote_reference or "unknown carrier"),
                    "quote_reference": quote.quote_reference,
                    "version": quote.version,
                    "currency": quote.currency,
                    "base_price": str(quote.base_price) if quote.base_price is not None else None,
                    "total_known_cost": str(quote.total_known_cost) if quote.total_known_cost is not None else None,
                    "valid_until": str(quote.valid_until) if quote.valid_until else None,
                    "delivery_scope": str(quote.delivery_scope) if quote.delivery_scope else None,
                    "comparison_status": str(quote.comparison_status),
                    "review_status": str(quote.review_status),
                    "reviewed_at": _iso(quote.reviewed_at),
                    "imported_at": _iso(quote.imported_at),
                    "review_notes": quote.review_notes,
                    "intake_item_id": str(quote.intake_item_id),
                    "review_evidence": [{
                        "field": e.field_name, "value": e.value_as_extracted,
                        "snippet": e.snippet, "status": str(e.validation_status),
                        "file": e.source_filename,
                    } for e in evidence],
                    "gaps": [{
                        "id": str(g.id), "field": g.field_name, "type": str(g.gap_type),
                        "severity": str(g.severity), "description": g.description,
                        "question": g.question_text,
                    } for g in gaps if g.quotation_id is None or str(g.quotation_id) == str(quote.id)],
                })
            drafts = [d for d in all_drafts if d.status in OPEN_DRAFT_STATES]
            replies = [r for r in repos.list_replies(str(shipment.id))]
            comparison = None
            comparison_error = None
            if quotes:
                try:
                    comparison = build_comparison(settings, str(shipment.id)).model_dump(mode="json")
                except Exception as exc:  # noqa: BLE001 — a comparison failure must not hide the rest of the page
                    comparison_error = str(exc)
            shipment_cards.append({
                "id": str(shipment.id),
                "reference": shipment.reference,
                "label": shipment_label(shipment),
                "route": route_label(shipment.origin, shipment.destination),
                "name": shipment.name,
                "origin": shipment.origin,
                "destination": shipment.destination,
                "route_category": shipment.route_category,
                "status": str(shipment.status),
                "cargo": shipment.cargo_description,
                "weight_kg": str(shipment.weight_kg) if shipment.weight_kg is not None else None,
                "volume_m3": str(shipment.volume_m3) if shipment.volume_m3 is not None else None,
                "required_delivery_scope": str(shipment.required_delivery_scope),
                "target_currency": shipment.target_currency,
                "updated_at": _iso(shipment.updated_at),
                "approved_at": _iso(shipment.approved_at),
                "route_evidence": json.loads(shipment.route_evidence_json or "[]"),
                "quotes": quotes,
                "open_gaps": len([g for g in gaps if g.quotation_id is not None]),
                "drafts": [{
                    "id": str(d.id), "subject": d.subject, "body": d.body,
                    "recipient": d.recipient, "status": str(d.status),
                    "quotation_id": str(d.quotation_id),
                } for d in drafts],
                "replies": [{
                    "id": str(r.id), "quotation_id": str(r.quotation_id),
                    "association": str(r.association_method), "created_at": _iso(r.created_at),
                } for r in replies],
                "comparison": comparison,
                "comparison_error": comparison_error,
            })

        activity = [{"entity": a.entity_type, "action": a.action, "at": _iso(a.created_at),
                     "text": _activity_text(a.entity_type, a.action,
                                            names.get(f"{a.entity_type}:{a.entity_id}"))}
                    for a in repos.list_recent_audit(12)]

    quotes = [q for s in shipment_cards for q in s["quotes"]]
    counts = {
        "documents": len(documents),
        "proposals": len([s for s in shipment_cards if s["status"] == str(ShipmentStatus.needs_review)]),
        "drafts": len([d for s in shipment_cards for d in s["drafts"]]),
        "gaps": len([g for s in shipment_cards for q in s["quotes"] for g in q["gaps"]]),
        "awaiting_review": len([q for q in quotes if q["review_status"] == "extracted"]),
        "ready_to_import": len([q for q in quotes if q["review_status"] == "approved"]),
        "imported": len([q for q in quotes if q["review_status"] == "imported"]),
    }
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": counts, "documents": documents, "shipments": shipment_cards, "activity": activity}


def approve_review(settings: Settings, quotation_id: str, notes: str | None = None) -> Quotation:
    """Record the human review verdict; the canonical commit stays a separate step."""
    with with_session(settings) as repos, repos.atomic():
        quote = repos.get_quotation(quotation_id)
        if quote.review_status in {ReviewStatus.imported}:
            raise ValidationFailureError("This quotation is already imported; reprocessing creates a new version to review.")
        if quote.review_status == ReviewStatus.approved:
            return quote
        stamp = _now()
        approved = quote.model_copy(update={
            "review_status": ReviewStatus.approved, "reviewed_at": stamp, "updated_at": stamp,
            "review_notes": notes or quote.review_notes,
        })
        repos.update_quotation(approved)
        repos.add_audit(AuditEvent(entity_type="quotation", entity_id=approved.id, action="review_approved",
                                   details_json=json.dumps({"version": approved.version, "notes": notes})))
        return approved


def reject_review(settings: Settings, quotation_id: str, reason: str | None) -> Quotation:
    with with_session(settings) as repos, repos.atomic():
        quote = repos.get_quotation(quotation_id)
        if quote.review_status == ReviewStatus.imported:
            raise ValidationFailureError("An imported quotation cannot be rejected; correct it and reprocess instead.")
        stamp = _now()
        rejected = quote.model_copy(update={
            "review_status": ReviewStatus.rejected, "reviewed_at": stamp, "updated_at": stamp,
            "review_notes": reason or quote.review_notes,
        })
        repos.update_quotation(rejected)
        repos.add_audit(AuditEvent(entity_type="quotation", entity_id=rejected.id, action="review_rejected",
                                   details_json=json.dumps({"reason": reason})))
        return rejected


def preview_import(settings: Settings, quotation_id: str) -> dict[str, Any]:
    """Describe exactly what the explicit import will commit, without writing anything."""
    with with_session(settings) as repos:
        quote = repos.get_quotation(quotation_id)
        shipment = repos.get_shipment(str(quote.shipment_id))
        evidence = repos.evidence_for_quotation(str(quote.id))
        gaps = [g for g in repos.list_gaps(str(shipment.id))
                if g.status == GapStatus.open and (g.quotation_id is None or str(g.quotation_id) == str(quote.id))]
        drafts = [d for d in repos.list_drafts(str(shipment.id)) if str(d.quotation_id) == str(quote.id)]
        critical = [g for g in gaps if str(g.severity) == "critical"]
        unreviewed = [e for e in evidence if e.validation_status != ValidationStatus.accepted]
        return {
            "quotation_id": str(quote.id),
            "version": quote.version,
            "review_status": str(quote.review_status),
            "already_imported": quote.review_status == ReviewStatus.imported,
            "creates": [] if quote.version > 1 else [{"record": "quotation", "id": str(quote.id)}],
            "updates": [{"record": "quotation", "id": str(quote.id),
                         "from": str(quote.review_status), "to": str(ReviewStatus.imported)}],
            "linked": {
                "shipment": f"{shipment_label(shipment)} · {str(shipment.status).replace('_', ' ')}",
                "carrier_evidence": len(evidence),
                "open_gaps": len(gaps),
                "critical_gaps": len(critical),
                "unreviewed_evidence": len(unreviewed),
                "drafts": len(drafts),
            },
            "warnings": ([] if quote.review_status in {ReviewStatus.approved, ReviewStatus.imported}
                         else ["Approve the review first — importing only commits an approved extraction"])
                        + ([f"{len(critical)} item(s) still missing that block a fair price comparison"]
                           if critical else [])
                        + ([f"{len(unreviewed)} extracted value(s) not yet confirmed or currently flagged for review"]
                           if unreviewed else []),
        }


def import_reviewed(settings: Settings, quotation_id: str) -> dict[str, Any]:
    """Commit an approved extraction to the canonical record; safe to call twice."""
    with with_session(settings) as repos, repos.atomic():
        quote = repos.get_quotation(quotation_id)
        if quote.review_status == ReviewStatus.imported:
            return {"already_imported": True, "quotation_id": str(quote.id), "version": quote.version,
                    "message": f"quotation {quote.id} v{quote.version} was already imported — nothing changed"}
        if quote.review_status != ReviewStatus.approved:
            raise ValidationFailureError(
                "Approve the review before importing; import only commits an approved extraction.")
        stamp = _now()
        imported = quote.model_copy(update={"review_status": ReviewStatus.imported, "imported_at": stamp,
                                            "updated_at": stamp})
        repos.update_quotation(imported)
        repos.add_audit(AuditEvent(entity_type="quotation", entity_id=imported.id, action="imported_to_database",
                                   details_json=json.dumps({"version": imported.version,
                                                             "shipment_id": str(imported.shipment_id)})))
        return {"already_imported": False, "quotation_id": str(imported.id), "version": imported.version,
                "shipment_id": str(imported.shipment_id),
                "message": f"quotation {imported.id} v{imported.version} imported to the database"}


def set_evidence_status(settings: Settings, evidence_id: str, accept: bool) -> FieldEvidence:
    with with_session(settings) as repos, repos.atomic():
        evidence = repos.get_evidence(evidence_id)
        updated = evidence.model_copy(update={
            "validation_status": ValidationStatus.accepted if accept else ValidationStatus.needs_review})
        repos.update_evidence(updated)
        repos.add_audit(AuditEvent(entity_type="field_evidence", entity_id=updated.id,
                                   action="evidence_accepted" if accept else "evidence_rejected",
                                   details_json=json.dumps({"field_name": updated.field_name})))
        return updated


def _mark_intake_irrelevant(settings: Settings, intake_id: str, reason: str | None) -> IntakeItem:
    with with_session(settings) as repos, repos.atomic():
        item = repos.get_intake(intake_id)
        already_irrelevant = item.classification_status == ClassificationStatus.irrelevant
        updated_reason = reason if reason and reason.strip() else item.classification_reason
        updated_reason = updated_reason or "marked irrelevant in the review console"
        updated = item.model_copy(update={
            "classification_status": ClassificationStatus.irrelevant,
            "classification_reason": updated_reason,
            "processing_status": ProcessingStatus.processed if item.processing_status == ProcessingStatus.pending else item.processing_status,
        })
        repos.update_intake(updated)
        if not already_irrelevant:
            repos.add_audit(AuditEvent(entity_type="intake_item", entity_id=item.id, action="marked_irrelevant",
                                       details_json=json.dumps({"reason": updated_reason})))
        return updated


def apply_action(settings: Settings, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one review-console action onto the workflow services."""
    from .clarification import approve_draft, export_draft, generate_clarification_drafts
    from .comparison import export_report, waive_gap
    from .extraction import extract_and_store_quote
    from .replies import process_supplier_reply
    from .shipments import (approve_proposed_shipment, edit_proposed_shipment, get_shipment,
                            reject_proposed_shipment)

    entity_id = str(payload.get("id") or "").strip()
    if action != "refresh" and not entity_id:
        raise ValidationFailureError(f"{action}: missing record id")

    if action == "intake.process":
        summary = extract_and_store_quote(settings, entity_id, force=bool(payload.get("force")))
        return {"message": f"processed {summary.intake_item_id}", "notes": summary.notes,
                "shipment_id": str(summary.shipment_id) if summary.shipment_id else None}
    if action == "intake.ignore":
        already_irrelevant = False
        with with_session(settings) as repos:
            already_irrelevant = repos.get_intake(entity_id).classification_status == ClassificationStatus.irrelevant
        item = _mark_intake_irrelevant(settings, entity_id, payload.get("reason"))
        return {"message": (f"{item.original_filename} is already marked as not a quote; the existing disposition was retained"
                            if already_irrelevant
                            else f"{item.original_filename} marked as not a quote and retained for audit")}
    if action == "shipment.approve":
        current = get_shipment(settings, entity_id)
        approved = approve_proposed_shipment(settings, entity_id, payload.get("expected_updated_at")
                                             or current.updated_at)
        notes: list[str] = []
        try:
            drafts = generate_clarification_drafts(settings, str(approved.id))
            notes = [f"clarification draft ready: {d.id}" for d in drafts] or ["no open gaps: no drafts needed"]
        except Exception as exc:  # noqa: BLE001 — approval stands; drafts can be regenerated later
            notes = [f"draft generation failed, retry `freight questions generate last`: {exc}"]
        return {"message": f"{approved.reference} approved and open", "notes": notes}
    if action == "shipment.reject":
        rejected = reject_proposed_shipment(settings, entity_id, payload.get("reason"))
        return {"message": f"{rejected.reference} rejected and closed (future documents will not match it)"}
    if action == "shipment.edit":
        changes = {k: v for k, v in (payload.get("fields") or {}).items() if v not in (None, "")}
        if not changes:
            raise ValidationFailureError("shipment.edit: no fields to change")
        edited = edit_proposed_shipment(settings, entity_id, **changes)
        return {"message": f"{edited.reference} corrected"}
    if action == "shipment.questions":
        drafts = generate_clarification_drafts(settings, entity_id)
        return {"message": f"{len(drafts)} draft(s) prepared"}
    if action == "gap.waive":
        gap = waive_gap(settings, entity_id, (payload.get("reason") or "").strip())
        return {"message": f"gap on {gap.field_name} waived (audited)"}
    if action == "draft.approve":
        draft = approve_draft(settings, entity_id)
        return {"message": f"draft approved: {draft.subject}"}
    if action == "draft.export":
        path = export_draft(settings, entity_id, format=payload.get("format") or "eml",
                            recipient=payload.get("recipient") or None)
        return {"message": f"exported locally, nothing sent: {path}"}
    if action == "reply.process":
        quote = process_supplier_reply(settings, entity_id)
        return {"message": f"reply applied: quotation {quote.id} v{quote.version}"}
    if action == "report.export":
        path = export_report(settings, entity_id, fmt=payload.get("format") or "md",
                             output_path=Path(payload["output"]) if payload.get("output") else None)
        return {"message": f"comparison report written: {path}"}
    if action == "review.approve":
        quote = approve_review(settings, entity_id, payload.get("notes"))
        return {"message": f"review approved for quotation {quote.id} v{quote.version} — import is still separate"}
    if action == "review.reject":
        quote = reject_review(settings, entity_id, payload.get("reason"))
        return {"message": f"quotation {quote.id} marked not relevant (audited); nothing was imported"}
    if action == "review.preview":
        preview = preview_import(settings, entity_id)
        return {"message": "import preview", "preview": preview}
    if action == "review.import":
        return import_reviewed(settings, entity_id)
    if action == "evidence.decide":
        evidence = set_evidence_status(settings, entity_id, bool(payload.get("accept")))
        return {"message": f"evidence for {evidence.field_name} "
                           f"{'accepted' if payload.get('accept') else 'flagged for review'}"}
    if action == "refresh":
        return {"message": "refreshed"}
    raise NotFoundError(f"unknown review action: {action}")


__all__ = ["apply_action", "approve_review", "build_review", "import_reviewed",
           "preview_import", "reject_review", "set_evidence_status", "shipment_label"]

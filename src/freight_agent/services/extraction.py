from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from ..config import Settings
from ..agents import prompts
from ..agents.quote_extractor import build_extractor_agent, run_extraction
from ..domain.enums import (
    ClassificationStatus,
    ComparisonStatus,
    ProcessingStatus,
    ValidationStatus,
)
from ..domain.exceptions import LLMOutputInvalidError, ModelUnavailableError, ValidationFailureError
from ..domain.models import (
    AuditEvent,
    FieldEvidence,
    Gap,
    ProcessingSummary,
    Quotation,
)
from ..db.repositories import Repos
from .classification import classify_intake, truncate_for_llm
from .common import with_session
from .gap_analysis import (
    analyze_gaps_for_quote,
    comparison_status_after_gaps,
    detect_gaps_deterministic,
)
from .shipments import resolve_document_shipment
from .validation import compute_total_known_cost, validate_extraction


def extract_and_store_quote(settings: Settings, intake_id: str, dry_run: bool = False,
                            force: bool = False, shipment_reference: str | None = None) -> ProcessingSummary:
    """Workflow A: intake → classify → extract → validate → persist quote + evidence → gaps."""
    with with_session(settings) as repos:
        item = repos.get_intake(intake_id)
        intake_id = str(item.id)
        initial_link = item.shipment_id
        selected_shipment = repos.get_shipment(shipment_reference) if shipment_reference is not None else None
        if selected_shipment is not None and item.shipment_id not in (None, selected_shipment.id):
            raise ValidationFailureError("Intake is already linked to another shipment; cannot reassign it during processing.")
        notes: list[str] = []

        existing_quote = repos.quotation_for_intake(intake_id)
        if (existing_quote is not None and selected_shipment is not None
                and existing_quote.shipment_id != selected_shipment.id):
            raise ValidationFailureError("Existing quotation belongs to another shipment; cannot reassign it during processing.")
        if existing_quote is not None and not force:
            raise ValidationFailureError(
                f"intake {str(item.id)[:8]} already produced quotation {str(existing_quote.id)[:8]} "
                f"(v{existing_quote.version}); use --force or `freight quote reprocess` "
                "to reprocess without silently duplicating")

        # 1. classify if needed
        if item.classification_status == ClassificationStatus.pending:
            item, classification = classify_intake(settings, intake_id, dry_run=dry_run)
        else:
            classification = _load_classification(settings, item)

        if item.classification_status == ClassificationStatus.irrelevant:
            if not force:
                updated = item.model_copy(update={"processing_status": ProcessingStatus.processed})
                if not dry_run:
                    repos.update_intake(updated)
                    repos.add_audit(AuditEvent(entity_type="intake_item", entity_id=item.id,
                                               action="rejected", details_json=json.dumps(
                        {"reason": item.classification_reason})))
                return ProcessingSummary(
                    intake_item_id=item.id, classification=classification,
                    notes=["Reading stopped: the document was classified as not a quote. " + (item.classification_reason or "")],
                )
            notes.append("forced processing of an irrelevant classification (human override, audited)")

        if item.classification_status in (ClassificationStatus.needs_review,
                                         ClassificationStatus.failed) and not force:
            processing_status = (ProcessingStatus.needs_review
                                 if item.classification_status == ClassificationStatus.needs_review
                                 else ProcessingStatus.failed)
            gate_item = item.model_copy(update={"processing_status": processing_status})
            if not dry_run:
                repos.update_intake(gate_item)
            return ProcessingSummary(
                intake_item_id=item.id, classification=classification,
                notes=["Reading stopped at classification review; no quote was extracted. "
                       "Review the classification decision, then choose Read again to retry with an explicit override."],
            )

        if selected_shipment is not None and item.shipment_id is None:
            item = item.model_copy(update={"shipment_id": selected_shipment.id})
            notes.append(f"considering explicit shipment: {selected_shipment.reference} "
                         f"(linked only if its route matches the document)")
        if force and item.classification_status in (ClassificationStatus.needs_review, ClassificationStatus.failed):
            notes.append("forced processing of a needs_review/failed classification (human override, audited)")
            if not dry_run:
                repos.add_audit(AuditEvent(
                    entity_type="intake_item", entity_id=item.id, action="classification_gate_overridden",
                    details_json=json.dumps({"classification_status": str(item.classification_status)}),
                ))

        processing_update = item.model_copy(update={"processing_status": ProcessingStatus.processing})
        if not dry_run:
            repos.update_intake(processing_update)

        # 2. extraction agent
        try:
            agent = build_extractor_agent(settings)
            extraction = run_extraction(agent, item.original_filename,
                                        truncate_for_llm(item.raw_text, settings), settings)
        except LLMOutputInvalidError as exc:
            failed = item.model_copy(update={
                "processing_status": ProcessingStatus.failed,
                "classification_status": item.classification_status,
            })
            if not dry_run:
                repos.update_intake(failed)
                repos.add_audit(AuditEvent(entity_type="intake_item", entity_id=item.id,
                                           action="extraction_failed",
                                           details_json=json.dumps({"error": str(exc)[:500]})))
            raise

        # 3. domain validation
        try:
            cleaned, evidence_items, field_statuses, issues = validate_extraction(
                extraction, item.raw_text, item.original_filename)
        except ValidationFailureError as exc:
            invalid = LLMOutputInvalidError(f"extraction failed domain validation: {exc}")
            failed = item.model_copy(update={
                "processing_status": ProcessingStatus.failed,
                "classification_status": item.classification_status,
            })
            if not dry_run:
                repos.update_intake(failed)
                repos.add_audit(AuditEvent(entity_type="intake_item", entity_id=item.id,
                                           action="extraction_failed",
                                           details_json=json.dumps({"error": str(invalid)[:500]})))
            raise invalid from exc
        notes += issues

        # 4. autonomous shipment resolution: match an existing route, propose a new
        #    one for human approval, or hand back for review — never silently reassign.
        shipment, is_proposal, route_notes = resolve_document_shipment(
            repos, item, cleaned, explicit=selected_shipment,
            reference=getattr(classification, "shipment_reference", None))
        notes += route_notes
        if shipment is None:
            review_item = item.model_copy(update={
                "processing_status": ProcessingStatus.needs_review,
                "shipment_id": initial_link})
            if not dry_run:
                with repos.atomic():
                    repos.update_intake(review_item)
                    repos.add_audit(AuditEvent(
                        entity_type="intake_item", entity_id=item.id,
                        action="route_review_required",
                        details_json=json.dumps({"notes": route_notes})))
            return ProcessingSummary(
                intake_item_id=item.id, classification=classification,
                notes=notes + ["No quotation created — resolve the shipment/route, then re-run "
                               "`freight intake process last --force` after fixing the cause."],
            )
        shipment_id = shipment.id

        carrier = repos.get_or_create_carrier(cleaned.carrier_name.strip()) if cleaned.carrier_name else None

        total_known, total_notes = compute_total_known_cost(
            cleaned.base_price, cleaned.currency, cleaned.charges)
        notes += total_notes

        from ..domain.enums import DeliveryScope
        quote = Quotation(
            shipment_id=shipment_id,  # type: ignore[arg-type]
            carrier_id=carrier.id if carrier else None,
            intake_item_id=item.id,
            quote_reference=cleaned.quote_reference,
            currency=cleaned.currency,
            base_price=cleaned.base_price,
            known_additional_charges=cleaned.charges,
            included_services=cleaned.included_services,
            excluded_services=cleaned.excluded_services,
            origin=cleaned.origin, destination=cleaned.destination,
            transit_days_min=cleaned.transit_days_min, transit_days_max=cleaned.transit_days_max,
            valid_until=cleaned.valid_until,
            delivery_scope=DeliveryScope(cleaned.delivery_scope) if cleaned.delivery_scope else None,
            insurance_included=cleaned.insurance_included,
            customs_included=cleaned.customs_included,
            total_known_cost=total_known,
            comparison_status=ComparisonStatus.needs_review if cleaned.fields_requiring_review
            else ComparisonStatus.comparable,
            assumptions=cleaned.assumptions,
            model_name=settings.ollama_model,
            prompt_version=prompts.PROMPT_VERSION,
            processed_at=datetime.now(timezone.utc).replace(microsecond=0),
        )

        # 5. gaps (deterministic) feed the final comparison status
        gaps_items = detect_gaps_deterministic(shipment, quote)  # type: ignore[arg-type]
        quote.comparison_status = comparison_status_after_gaps(quote, gaps_items)

        if existing_quote is not None:
            # reprocessing keeps quote identity and bumps version (field provenance history)
            quote.id = existing_quote.id
            quote.version = existing_quote.version + 1
            quote.created_at = existing_quote.created_at

        evidence_snippets = {e.field_name: e.snippet for e in evidence_items}
        analysis = analyze_gaps_for_quote(settings, shipment, quote, repos,  # type: ignore[arg-type]
                                          evidence_snippets, cleaned.carrier_name)
        quote.comparison_status = comparison_status_after_gaps(quote, gaps_items)

        evidence_rows = [
            FieldEvidence(
                quotation_id=quote.id, field_name=e.field_name,
                value_as_extracted=str(_field_value(quote, e.field_name)),
                source_filename=item.original_filename,
                page_or_sheet=e.page_or_sheet, snippet=e.snippet, confidence=e.confidence,
                validation_status=field_statuses.get(e.field_name, ValidationStatus.accepted),
            ) for e in evidence_items
        ]
        for field in cleaned.fields_requiring_review:
            if not any(e.field_name == field for e in evidence_rows):
                evidence_rows.append(FieldEvidence(
                    quotation_id=quote.id, field_name=field, value_as_extracted="(no evidence)",
                    source_filename=item.original_filename, snippet="missing evidence — flagged by validator",
                    validation_status=ValidationStatus.needs_review,
                ))

        gap_rows = [
            Gap(shipment_id=shipment_id, quotation_id=quote.id,  # type: ignore[arg-type]
                field_name=g.field_name, gap_type=g.gap_type, severity=g.severity,
                description=g.description,
                question_text=next((q.question_text for q in analysis.clarification_questions
                                    if q.field_name == g.field_name), None))
            for g in gaps_items
        ]

        processed_item = item.model_copy(update={
            "processing_status": ProcessingStatus.needs_review if is_proposal
            else ProcessingStatus.processed,
            "shipment_id": shipment_id,
        })

        if dry_run:
            return ProcessingSummary(
                intake_item_id=item.id, classification=classification,
                shipment_id=shipment_id, shipment_status=shipment.status,
                quotation_id=None, created_quote=False,
                gap_count=len(gap_rows),
                critical_gap_count=sum(1 for g in gaps_items if str(g.severity) == "critical"),
                comparison_status=quote.comparison_status,
                notes=["DRY-RUN: nothing persisted. Quote preview: "
                       + quote.model_dump_json(indent=2)[:1500]],
            )

        with repos.atomic():
            if is_proposal:
                repos.add_shipment(shipment)
                repos.add_audit(AuditEvent(
                    entity_type="shipment", entity_id=shipment.id, action="auto_proposed",
                    details_json=json.dumps({
                        "intake_item_id": str(item.id),
                        "route_category": shipment.route_category,
                        "source": "document_route_extraction"})))
            elif (shipment.origin_country or shipment.destination_country) and processed_item.shipment_id != initial_link:
                repos.update_shipment(shipment.model_copy(update={
                    "updated_at": datetime.now(timezone.utc).replace(microsecond=0)}))
            if processed_item.shipment_id != initial_link:
                repos.add_audit(AuditEvent(
                    entity_type="intake_item", entity_id=item.id, action="linked_to_shipment",
                    details_json=json.dumps({"shipment_id": str(processed_item.shipment_id)})))
            if existing_quote is None:
                repos.add_quotation(quote)
            else:
                repos.record_quotation_version(existing_quote, repos.evidence_for_quotation(str(quote.id)))
                repos.delete_evidence_for_quotation(str(quote.id))
                for old_gap in repos.list_gaps(str(shipment_id)):
                    if old_gap.quotation_id == quote.id and str(old_gap.status) == "open":
                        from ..domain.enums import GapStatus
                        old_gap.status = GapStatus.resolved
                        old_gap.resolved_at = quote.updated_at
                        repos.update_gap(old_gap)
                repos.update_quotation(quote)
            repos.add_evidence(evidence_rows)
            repos.add_gaps(gap_rows)
            repos.record_quotation_version(quote, evidence_rows)
            repos.update_intake(processed_item)
            repos.add_audit(AuditEvent(entity_type="quotation", entity_id=quote.id,
                                       action="created_from_intake" if existing_quote is None
                                       else f"reprocessed_from_intake_v{quote.version}",
                                       details_json=json.dumps({
                                           "intake_item_id": str(item.id), "model": settings.ollama_model,
                                           "prompt_version": prompts.PROMPT_VERSION,
                                           "readiness": str(analysis.comparison_readiness)})))

        if is_proposal:
            notes.append("Clarification drafts are generated automatically once the proposed shipment is "
                         "approved: `freight shipment approve last -y` (or the Approve button in `freight review`).")
        else:
            from .clarification import generate_clarification_drafts
            try:
                drafts = generate_clarification_drafts(settings, str(shipment_id))
            except ModelUnavailableError as exc:
                notes.append(
                    f"Quotation {quote.id} was saved. Clarification wording could not be generated; "
                    f"retry it later for shipment {shipment.reference}."
                )
            notes.extend(f"clarification draft awaiting approval: {draft.id}" for draft in drafts)

        return ProcessingSummary(
            intake_item_id=item.id, classification=classification,
            shipment_id=shipment_id, shipment_status=shipment.status,
            quotation_id=quote.id, created_quote=True,
            gap_count=len(gap_rows),
            critical_gap_count=sum(1 for g in gaps_items if str(g.severity) == "critical"),
            comparison_status=quote.comparison_status, notes=notes,
        )


def _field_value(quote: Quotation, field: str):
    return getattr(quote, field, None)


def _load_classification(settings: Settings, item) -> "object | None":
    from ..domain.models import ClassificationResult
    from ..db.engine import get_session as _gs
    from ..db import models as _m
    session = _gs(settings)
    try:
        row = session.get(_m.IntakeItemRow, str(item.id))
        if row and row.classification_json:
            return ClassificationResult.model_validate_json(row.classification_json)
        return None
    finally:
        session.close()

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx2
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from ..agents import prompts
from ..agents.model import make_agent
from ..config import Settings
from ..domain.enums import (
    AssociationMethod, ClassificationStatus, ComparisonStatus, DeliveryScope,
    GapSeverity, GapStatus, GapType, ProcessingStatus, SourceType, ValidationStatus,
)
from ..domain.exceptions import (
    DuplicateIntakeError, FreightAgentError, LLMOutputInvalidError, ModelUnavailableError,
    ReplyAssociationRequiredError, ValidationFailureError,
)
from ..domain.models import (
    AuditEvent, FieldEvidence, Gap, IntakeItem, Quotation, QuoteExtraction, SupplierReply,
)
from ..parsers.base import ParseResult, parse_file
from .classification import truncate_for_llm
from .common import with_session
from .gap_analysis import comparison_status_after_gaps, detect_gaps_deterministic
from .intake import register_intake, sha256_file
from .validation import MATERIAL_FIELDS, compute_total_known_cost, snippet_grounded, validate_extraction


REPLY_EXTENSIONS = {".txt", ".eml", ".pdf"}


def _new_reply_text(text: str) -> str:
    boundary = re.search(r"(?im)^\s*(?:[-_]{2,}\s*Original Message\s*[-_]{2,}|On .{1,500}wrote:|From:.*\nSent:)", text)
    if boundary:
        text = text[:boundary.start()]
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def _parse_reply(settings: Settings, path: Path) -> ParseResult:
    if not path.is_file() or path.suffix.lower() not in REPLY_EXTENSIONS:
        raise ValidationFailureError("supplier replies must be local TXT, EML or PDF files")
    if path.stat().st_size > settings.max_file_size_mb * 1024 * 1024:
        raise ValidationFailureError(f"reply exceeds MAX_FILE_SIZE_MB={settings.max_file_size_mb}")
    return parse_file(path)


def _contains_reference(reference: str, text: str) -> bool:
    return bool(reference and re.search(r"(?<![\w-])" + re.escape(reference) + r"(?![\w-])", text, re.I))


def _associate(repos, shipment, parsed: ParseResult, carrier_id: str | None,
               quote_id: str | None) -> tuple[Quotation, AssociationMethod]:
    quotes = repos.list_quotations(str(shipment.id))
    if carrier_id:
        candidates = []
        for quote in quotes:
            carrier = repos.get_carrier(str(quote.carrier_id)) if quote.carrier_id else None
            if carrier and (str(carrier.id) == carrier_id or carrier.name.casefold() == carrier_id.casefold()):
                candidates.append(quote)
        if not candidates:
            raise ValidationFailureError("provided carrier has no quotations for this shipment")
    else:
        candidates = quotes
    if parsed.metadata.get("shipment_id") and parsed.metadata["shipment_id"] != str(shipment.id):
        raise ValidationFailureError("reply shipment header does not match the selected shipment")
    if quote_id:
        quote = repos.get_quotation(quote_id)
        if quote.shipment_id != shipment.id or quote.id not in {q.id for q in candidates}:
            raise ValidationFailureError("quotation, carrier and shipment do not match")
        return quote, AssociationMethod.manual
    text = (parsed.subject or "") + "\n" + parsed.text
    header_id = parsed.metadata.get("quotation_id")
    matches = [q for q in quotes if (
        header_id == str(q.id) or _contains_reference(str(q.id), text)
        or (q.quote_reference and _contains_reference(q.quote_reference, text))
    )]
    if len(matches) == 1 and matches[0].id in {q.id for q in candidates}:
        return matches[0], AssociationMethod.explicit_reference
    if matches or header_id:
        raise ReplyAssociationRequiredError(matches or candidates)
    if carrier_id and len(candidates) == 1:
        return candidates[0], AssociationMethod.manual
    raise ReplyAssociationRequiredError(candidates)


def import_supplier_reply(settings: Settings, shipment_id: str, path: Path,
                          carrier_id: str | None = None, quote_id: str | None = None,
                          on_duplicate: str = "error", dry_run: bool = False) -> SupplierReply:
    if dry_run:
        return preview_supplier_reply(settings, shipment_id, path, carrier_id, quote_id, on_duplicate)[0]
    if on_duplicate not in {"error", "skip", "reprocess"}:
        raise ValidationFailureError("on_duplicate must be error, skip or reprocess")
    parsed = _parse_reply(settings, path)
    digest = sha256_file(path)
    with with_session(settings) as repos:
        shipment = repos.get_shipment(shipment_id)
        quote, method = _associate(repos, shipment, parsed, carrier_id, quote_id)
        duplicate = repos.find_intake_by_sha(digest)
        if duplicate and on_duplicate != "reprocess":
            previous = repos.reply_for_intake(str(duplicate.id))
            if on_duplicate == "skip" and previous and previous.quotation_id == quote.id:
                return previous
            raise DuplicateIntakeError(digest, str(duplicate.id), duplicate.original_filename)
    item, _ = register_intake(settings, str(shipment.id), path, source="imported_reply",
                              on_duplicate=on_duplicate)
    reply = SupplierReply(shipment_id=shipment.id, carrier_id=quote.carrier_id,
                          quotation_id=quote.id, intake_item_id=item.id, association_method=method)
    with with_session(settings) as repos:
        with repos.atomic():
            repos.add_reply(reply)
            repos.add_audit(AuditEvent(entity_type="supplier_reply", entity_id=reply.id,
                                      action="imported", details_json=json.dumps({
                                          "quotation_id": str(quote.id), "association_method": str(method)})))
    _process_reply(settings, reply, item)
    return reply


def preview_supplier_reply(settings: Settings, shipment_id: str, path: Path,
                           carrier_id: str | None = None, quote_id: str | None = None,
                           on_duplicate: str = "error") -> tuple[SupplierReply, Quotation]:
    if on_duplicate not in {"error", "skip", "reprocess"}:
        raise ValidationFailureError("on_duplicate must be error, skip or reprocess")
    parsed = _parse_reply(settings, path)
    digest = sha256_file(path)
    with with_session(settings) as repos:
        shipment = repos.get_shipment(shipment_id)
        quote, method = _associate(repos, shipment, parsed, carrier_id, quote_id)
        duplicate = repos.find_intake_by_sha(digest)
        if duplicate and on_duplicate != "reprocess":
            previous = repos.reply_for_intake(str(duplicate.id))
            if on_duplicate == "skip" and previous and previous.quotation_id == quote.id:
                return previous, quote
            raise DuplicateIntakeError(digest, str(duplicate.id), duplicate.original_filename)
    item = IntakeItem(
        shipment_id=shipment.id, source_type=SourceType.imported_reply,
        original_filename=path.name, stored_path=str(path), content_sha256=digest,
        raw_text=parsed.text, sender=parsed.sender, subject=parsed.subject,
        page_or_sheet_info=parsed.page_or_sheet_info, file_size_bytes=path.stat().st_size,
    )
    reply = SupplierReply(shipment_id=shipment.id, carrier_id=quote.carrier_id,
                          quotation_id=quote.id, intake_item_id=item.id, association_method=method)
    return reply, _process_reply(settings, reply, item, dry_run=True)


def process_supplier_reply(settings: Settings, reply_id: str, dry_run: bool = False) -> Quotation:
    with with_session(settings) as repos:
        reply = repos.get_reply(reply_id)
        item = repos.get_intake(str(reply.intake_item_id))
    return _process_reply(settings, reply, item, dry_run)


def _extract_reply(settings: Settings, item: IntakeItem, quote: Quotation) -> QuoteExtraction:
    agent = make_agent(settings, QuoteExtraction, prompts.REPLY_SYSTEM, "reply_extractor")
    context = json.dumps({"existing_charge_labels": [c.label for c in quote.known_additional_charges]})
    prompt = prompts.wrap_untrusted(item.original_filename, truncate_for_llm(item.raw_text, settings), "supplier_reply")
    prompt += "\n" + prompts.wrap_untrusted("charge_labels.json", context, "quote_context")
    prompt += "\nExtract the ReplyExtraction now. Only explicit reply facts; absent fields stay null."
    try:
        return agent.run_sync(prompt).output
    except (UnexpectedModelBehavior, ValidationError) as exc:
        raise LLMOutputInvalidError("reply output failed schema validation after bounded retries; reprocess the reply") from exc
    except (httpx2.TimeoutException, httpx2.NetworkError, ModelHTTPError) as exc:
        raise ModelUnavailableError("local Ollama reply extraction failed; check `freight health` and reprocess the reply") from exc


def _merge_reply(quote: Quotation, extraction: QuoteExtraction, item: IntakeItem,
                 old_evidence: list[FieldEvidence], carrier_name: str | None = None
                 ) -> tuple[Quotation, list[FieldEvidence], list[Gap]]:
    cleaned, grounded, _, _ = validate_extraction(extraction, item.raw_text, item.original_filename)
    by_field = {ev.field_name: ev for ev in grounded}
    supplied = {field for field in MATERIAL_FIELDS
                if getattr(extraction, field, None) is not None and getattr(extraction, field, None) != []}
    if not supplied and not cleaned.charges:
        raise LLMOutputInvalidError("reply has no explicit quotation facts; item needs review and the quotation is unchanged")
    review = set(cleaned.fields_requiring_review) & supplied
    review.update(f for f in cleaned.fields_requiring_review if f.startswith("charges:"))
    merged = quote.model_copy(deep=True)
    evidence = [e.model_copy(deep=True) for e in old_evidence]

    def save_evidence(field: str, value, snippet: str, page=None, confidence=None,
                      status=ValidationStatus.accepted):
        nonlocal evidence
        if status == ValidationStatus.accepted:
            evidence = [e for e in evidence if e.field_name != field]
        evidence.append(FieldEvidence(
            quotation_id=quote.id, field_name=field, value_as_extracted=str(value),
            source_filename=item.original_filename, page_or_sheet=page,
            snippet=snippet, confidence=confidence, validation_status=status,
        ))

    if cleaned.delivery_scope:
        if cleaned.delivery_scope == "unknown":
            review.add("delivery_scope")
        try:
            DeliveryScope(cleaned.delivery_scope)
        except ValueError:
            review.add("delivery_scope")
    if cleaned.currency and quote.currency and cleaned.currency != quote.currency and cleaned.base_price is None:
        review.add("currency")
    for field in supplied:
        value = getattr(cleaned, field)
        ev = by_field.get(field)
        if field == "carrier_name" and carrier_name and value and value.strip().casefold() != carrier_name.strip().casefold():
            review.add(field)
        if field in review or not ev or value is None:
            review.add(field)
            save_evidence(field, getattr(extraction, field), ev.snippet if ev else "missing or ungrounded evidence",
                          status=ValidationStatus.needs_review)
            continue
        if field == "carrier_name":
            save_evidence(field, value, ev.snippet, ev.page_or_sheet, ev.confidence)
            continue
        if field == "delivery_scope":
            value = DeliveryScope(value)
        if field in {"included_services", "excluded_services"}:
            current = getattr(merged, field)
            value = list(dict.fromkeys(current + value))
            opposite = "excluded_services" if field == "included_services" else "included_services"
            stated = {s.casefold().strip() for s in getattr(cleaned, field)}
            setattr(merged, opposite, [s for s in getattr(merged, opposite) if s.casefold().strip() not in stated])
        setattr(merged, field, value)
        save_evidence(field, value, ev.snippet, ev.page_or_sheet, ev.confidence)

    charges = list(merged.known_additional_charges)
    for charge in cleaned.charges:
        field = "charges:" + charge.label
        if field in review or not charge.evidence_snippet or not snippet_grounded(charge.evidence_snippet, item.raw_text):
            review.add(field)
            save_evidence(field, charge.model_dump_json(), charge.evidence_snippet or "missing evidence",
                          status=ValidationStatus.needs_review)
            continue
        indices = [i for i, old in enumerate(charges) if old.label.strip().casefold() == charge.label.strip().casefold()]
        if len(indices) > 1:
            review.add(field)
            continue
        if indices:
            old = charges[indices[0]]
            changes = {key: value for key, value in charge.model_dump().items() if value is not None and key != "label"}
            charges[indices[0]] = old.model_copy(update=changes)
        else:
            charges.append(charge)
        save_evidence(field, charge.model_dump_json(), charge.evidence_snippet)
    merged.known_additional_charges = charges
    if (merged.transit_days_min is not None and merged.transit_days_max is not None
            and merged.transit_days_min > merged.transit_days_max):
        review.update({"transit_days_min", "transit_days_max"})
        merged.transit_days_min, merged.transit_days_max = quote.transit_days_min, quote.transit_days_max
        for ev in evidence:
            if ev.field_name in {"transit_days_min", "transit_days_max"} and ev.source_filename == item.original_filename:
                ev.validation_status = ValidationStatus.needs_review
    overlaps = {s.casefold().strip() for s in cleaned.included_services} & {s.casefold().strip() for s in cleaned.excluded_services}
    if overlaps:
        review.update({"included_services", "excluded_services"})
    merged.total_known_cost, _ = compute_total_known_cost(merged.base_price, merged.currency, charges)
    unresolved = {e.field_name for e in evidence if e.validation_status != ValidationStatus.accepted}
    unresolved.update(review)
    if quote.comparison_status == ComparisonStatus.needs_review and not any(
            e.validation_status != ValidationStatus.accepted for e in old_evidence):
        unresolved.add("extraction_review")
    merged.comparison_status = ComparisonStatus.needs_review if unresolved else ComparisonStatus.comparable
    merged.version += 1
    merged.updated_at = datetime.now(timezone.utc).replace(microsecond=0)
    merged.processed_at = merged.updated_at
    gaps = [Gap(shipment_id=quote.shipment_id, quotation_id=quote.id, field_name=field,
                gap_type=GapType.ambiguous, severity=GapSeverity.critical,
                description=f"{field} needs review; supplier evidence is missing, conflicting or invalid")
            for field in sorted(unresolved)]
    return merged, evidence, gaps


def _process_reply(settings: Settings, reply: SupplierReply, item: IntakeItem,
                   dry_run: bool = False) -> Quotation:
    with with_session(settings) as repos:
        quote = repos.get_quotation(str(reply.quotation_id))
        shipment = repos.get_shipment(str(reply.shipment_id))
        old_evidence = repos.evidence_for_quotation(str(quote.id))
        carrier = repos.get_carrier(str(quote.carrier_id)) if quote.carrier_id else None
    try:
        reply_content = item.model_copy(update={"raw_text": _new_reply_text(item.raw_text)})
        extraction = _extract_reply(settings, reply_content, quote)
        merged, evidence, review_gaps = _merge_reply(quote, extraction, reply_content, old_evidence, carrier.name if carrier else None)
    except (FreightAgentError, httpx2.TimeoutException, httpx2.NetworkError, ModelHTTPError) as exc:
        if not dry_run:
            with with_session(settings) as repos:
                with repos.atomic():
                    item.processing_status = ProcessingStatus.failed
                    item.classification_status = ClassificationStatus.needs_review
                    repos.update_intake(item)
                    repos.add_audit(AuditEvent(entity_type="supplier_reply", entity_id=reply.id,
                                              action="processing_failed", details_json=json.dumps({"error_type": type(exc).__name__})))
        raise
    merged.model_name = settings.ollama_model
    merged.prompt_version = prompts.REPLY_PROMPT_VERSION
    gap_items = detect_gaps_deterministic(shipment, merged)
    new_gaps = [Gap(shipment_id=shipment.id, quotation_id=quote.id,
                    **g.model_dump()) for g in gap_items] + review_gaps
    with with_session(settings) as repos:
        current = repos.get_quotation(str(quote.id))
        if current.version != quote.version:
            raise ValidationFailureError("quotation changed during reply extraction; reprocess against the current version")
        old_gaps = [g for g in repos.list_gaps(str(shipment.id)) if g.quotation_id == quote.id]
        waived = {g.field_name for g in old_gaps if g.status == GapStatus.waived}
        active = [g for g in new_gaps if g.field_name not in waived]
        merged.comparison_status = comparison_status_after_gaps(merged, [g for g in gap_items if g.field_name not in waived])
        if dry_run:
            return merged
        with repos.atomic():
            repos.record_quotation_version(quote, old_evidence)
            confirmed_carrier = next((e for e in evidence if e.field_name == "carrier_name"
                                      and e.source_filename == item.original_filename
                                      and e.validation_status == ValidationStatus.accepted), None)
            if merged.carrier_id is None and confirmed_carrier:
                merged.carrier_id = repos.get_or_create_carrier(confirmed_carrier.value_as_extracted.strip()).id
                reply.carrier_id = merged.carrier_id
                repos.update_reply(reply)
            repos.update_quotation(merged)
            repos.delete_evidence_for_quotation(str(quote.id))
            repos.add_evidence(evidence)
            keys = {(g.field_name, g.gap_type, g.description) for g in active}
            for old in old_gaps:
                key = (old.field_name, old.gap_type, old.description)
                if old.status == GapStatus.open and key not in keys:
                    old.status = GapStatus.resolved
                    old.resolved_at = merged.updated_at
                    repos.update_gap(old)
            existing = {(g.field_name, g.gap_type, g.description) for g in old_gaps if g.status == GapStatus.open}
            repos.add_gaps([g for g in active if (g.field_name, g.gap_type, g.description) not in existing])
            repos.record_quotation_version(merged, evidence, item.id)
            item.processing_status = ProcessingStatus.processed
            item.classification_status = ClassificationStatus.relevant
            item.classification_reason = "explicitly associated supplier clarification reply"
            repos.update_intake(item)
            repos.add_audit(AuditEvent(entity_type="quotation", entity_id=quote.id,
                                      action="updated_from_supplier_reply", details_json=json.dumps({
                                          "reply_id": str(reply.id), "intake_item_id": str(item.id),
                                          "version": merged.version, "model": settings.ollama_model,
                                          "prompt_version": prompts.REPLY_PROMPT_VERSION})))
    from .clarification import generate_clarification_drafts
    try:
        generate_clarification_drafts(settings, str(shipment.id))
    except ModelUnavailableError as exc:
        raise ModelUnavailableError(
            f"Reply {reply.id} updated quotation {quote.id} to v{merged.version}, "
            f"but follow-up wording failed. Retry `freight questions generate {shipment.id}`. {exc}"
        ) from exc
    return merged

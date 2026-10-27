from __future__ import annotations

import json
import os
import re
from contextlib import nullcontext
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses, parseaddr
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from ..agents import prompts
from ..agents.gap_agent import build_gap_agent, word_questions
from ..config import Settings
from ..db.repositories import Repos
from ..domain.enums import DraftStatus, GapSeverity, GapStatus, GapType, QuestionSetStatus, ValidationStatus
from ..domain.exceptions import DatabaseError, ValidationFailureError
from ..domain.models import (
    AuditEvent, ClarificationDraft, ClarificationQuestion, Gap, GapAnalysisResult,
    GapItem, QuestionSet, Quotation, Shipment,
)
from .common import with_session
from .gap_analysis import COMPARISON_SCHEMA, detect_gaps_deterministic, readiness
from .validation import snippet_grounded

_MATERIAL_FIELDS = {
    "currency", "base_price", "charges", "delivery_scope", "valid_until", "transit_days",
    "insurance_included", "customs_included", "origin", "destination",
    "included_services", "excluded_services",
}
_SEVERITIES = {field: severity for field, severity, _ in COMPARISON_SCHEMA}
_RANK = {GapSeverity.critical: 0, GapSeverity.important: 1, GapSeverity.informational: 2}
_TEMPLATES = {
    "currency": "Could you confirm the currency of this quotation?",
    "base_price": "Could you confirm the base freight price and what it includes?",
    "charges": "Could you confirm the amount, currency, applicability, and base-price inclusion of each unresolved additional charge?",
    "delivery_scope": "Could you confirm the delivery scope and whether the required shipment scope can be covered?",
    "valid_until": "Could you confirm the current validity date and renew the offer if it has expired?",
    "transit_days": "Could you confirm the expected minimum and maximum transit time in days?",
    "insurance_included": "Is insurance included? If not, could you confirm its additional cost?",
    "customs_included": "Could you confirm whether customs clearance and handling are included, and any additional charges?",
    "origin": "Could you confirm the origin covered by this quotation?",
    "destination": "Could you confirm the destination covered by this quotation?",
    "included_services": "Could you confirm which services are included in the quoted price?",
    "excluded_services": "Could you confirm the excluded services and any applicable additional charges?",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _field(name: str) -> str:
    if name in {"transit_days_min", "transit_days_max"}:
        return "transit_days"
    if name == "known_additional_charges" or re.match(r"^(?:charges|known_additional_charges)[:.\[]", name):
        return "charges"
    return name


def _key(gap: Gap | GapItem) -> tuple[str, GapType, str]:
    return _field(gap.field_name), gap.gap_type, gap.description


def _audit(repos: Repos, entity, action: str, **details) -> None:
    repos.add_audit(AuditEvent(
        entity_type="question_set" if isinstance(entity, QuestionSet) else "clarification_draft",
        entity_id=entity.id, action=action, details_json=json.dumps(details),
    ))


def _analysis(repos: Repos, shipment: Shipment, quote: Quotation
              ) -> tuple[GapAnalysisResult, list[Gap], list[Gap]]:
    evidence = repos.evidence_for_quotation(str(quote.id))
    history = [g for g in repos.list_gaps(str(shipment.id)) if g.quotation_id == quote.id]
    decisions = {(_field(g.field_name), g.gap_type) for g in history
                 if g.status == GapStatus.waived}
    reviewed = {_field(e.field_name) for e in evidence
                if e.validation_status == ValidationStatus.needs_review}
    candidates = [g for g in detect_gaps_deterministic(shipment, quote)
                  if _field(g.field_name) in _MATERIAL_FIELDS
                  # An internal FX configuration gap is not a carrier clarification.
                  and not (g.field_name == "currency" and g.gap_type == GapType.missing
                           and quote.currency is not None)]
    for field in sorted(reviewed & _MATERIAL_FIELDS):
        candidates.append(GapItem(
            field_name=field, gap_type=GapType.ambiguous,
            severity=_SEVERITIES.get(field, GapSeverity.important),
            description=f"{field} has evidence requiring human review; the extracted value is not confirmed.",
        ))

    def still_open(gap: Gap) -> bool:
        field = _field(gap.field_name)
        if gap.gap_type == GapType.missing:
            if field == "currency" and quote.currency is not None and field not in reviewed:
                return False
            return any(_field(g.field_name) == field and g.gap_type == GapType.missing for g in candidates)
        if gap.gap_type == GapType.expired:
            return quote.valid_until is not None and quote.valid_until < date.today()
        if gap.gap_type == GapType.scope_mismatch:
            return any(_field(g.field_name) == field and g.gap_type == GapType.scope_mismatch for g in candidates)
        if field == "charges" and field not in reviewed:
            return any(g.field_name == "charges" for g in candidates)
        # Manually identified ambiguity/conflict remains open until explicitly reviewed.
        return True

    resolved: list[Gap] = []
    for gap in history:
        if gap.status != GapStatus.open or _field(gap.field_name) not in _MATERIAL_FIELDS:
            continue
        if not still_open(gap):
            resolved.append(gap.model_copy(update={"status": GapStatus.resolved, "resolved_at": _now()}))
        else:
            candidates.append(GapItem(field_name=_field(gap.field_name), gap_type=gap.gap_type,
                                      severity=gap.severity, description=gap.description))

    unique: dict[tuple[str, GapType, str], GapItem] = {}
    for gap in candidates:
        if gap.severity == GapSeverity.informational or (_field(gap.field_name), gap.gap_type) in decisions:
            continue
        normalized = gap.model_copy(update={"field_name": _field(gap.field_name)})
        unique.setdefault(_key(normalized), normalized)
    gaps = list(unique.values())
    grouped: dict[str, list[GapItem]] = {}
    for gap in gaps:
        grouped.setdefault(gap.field_name, []).append(gap)
    snippets: dict[str, list[str]] = {}
    sources = repos.list_intakes(str(shipment.id), None)
    for item in evidence:
        if item.validation_status != ValidationStatus.rejected and any(
                source.original_filename == item.source_filename
                and snippet_grounded(item.snippet, source.raw_text) for source in sources):
            snippets.setdefault(_field(item.field_name), []).append(item.snippet)
    for charge in quote.known_additional_charges:
        if charge.evidence_snippet and any(snippet_grounded(charge.evidence_snippet, source.raw_text) for source in sources):
            snippets.setdefault("charges", []).append(charge.evidence_snippet)
    carrier = repos.get_carrier(str(quote.carrier_id)) if quote.carrier_id else None
    questions = [ClarificationQuestion(
        field_name=field, why_it_matters=" ".join(dict.fromkeys(g.description for g in items)),
        question_text=_TEMPLATES[field], priority=min((g.severity for g in items), key=_RANK.__getitem__),
        source_context="\n".join(dict.fromkeys(snippets.get(field, []))) or "not found",
        target_carrier_id=quote.carrier_id, target_carrier_name=carrier.name if carrier else None,
    ) for field, items in grouped.items()]
    questions.sort(key=lambda q: (_RANK[q.priority], q.field_name))
    existing_keys = {_key(g) for g in history if g.status == GapStatus.open}
    pending = [Gap(shipment_id=shipment.id, quotation_id=quote.id,
                   field_name=g.field_name, gap_type=g.gap_type, severity=g.severity,
                   description=g.description,
                   question_text=next(q.question_text for q in questions if q.field_name == g.field_name))
               for g in gaps if _key(g) not in existing_keys]
    return GapAnalysisResult(
        gaps=gaps, clarification_questions=questions, comparison_readiness=readiness(gaps),
        summary=f"{len(questions)} material clarification field(s) for this quotation.",
    ), pending, resolved


def _current_set(repos: Repos, question_set: QuestionSet) -> Quotation:
    quote = repos.get_quotation(str(question_set.quotation_id))
    if (question_set.status == QuestionSetStatus.superseded
            or question_set.quotation_version != quote.version
            or question_set.shipment_id != quote.shipment_id):
        raise ValidationFailureError("Question set is stale or superseded; generate clarification for the current quote version.")
    return quote


def _current_draft(repos: Repos, draft: ClarificationDraft) -> tuple[QuestionSet, Quotation]:
    question_set = repos.get_question_set(str(draft.question_set_id))
    quote = _current_set(repos, question_set)
    if (draft.status == DraftStatus.cancelled or draft.quotation_version != quote.version
            or draft.quotation_id != quote.id or draft.shipment_id != quote.shipment_id
            or draft.quotation_version != question_set.quotation_version):
        raise ValidationFailureError("Draft is cancelled or stale; generate clarification for the current quote version.")
    return question_set, quote


def _header(value: str, label: str) -> None:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValidationFailureError(f"{label} contains forbidden header control characters (including CR/LF).")


def _email(value: str) -> str:
    _header(value, "Recipient")
    addresses = getaddresses([value])
    if len(addresses) != 1:
        raise ValidationFailureError("Supply exactly one recipient email address.")
    _, address = parseaddr(value)
    # parseaddr alone silently truncates malformed addresses and address lists.
    raw = value.strip()
    if "<" in raw:
        complete = re.fullmatch(r"[^<>]*<([^<>]+)>\s*", raw)
        if complete is None or complete.group(1).strip() != address:
            raise ValidationFailureError("Supply a single valid recipient email address.")
    elif raw != address:
        raise ValidationFailureError("Supply a single valid recipient email address.")
    if (not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", address)
            or address.startswith(".") or ".@" in address or ".." in address):
        raise ValidationFailureError("Supply a single valid recipient email address.")
    return address


def _recipient(repos: Repos, quote: Quotation, carrier_id=None) -> str | None:
    intake = repos.get_intake(str(quote.intake_item_id))
    if intake.sender:
        try:
            return _email(intake.sender)
        except ValidationFailureError:
            pass  # Unsafe/unusable intake headers are never carried into a draft.
    carrier = repos.get_carrier(str(carrier_id or quote.carrier_id)) if carrier_id or quote.carrier_id else None
    if carrier and carrier.contact_email:
        try:
            return _email(carrier.contact_email)
        except ValidationFailureError:
            pass
    return None


def _body(shipment: Shipment, question_set: QuestionSet) -> str:
    # Only deliberately selected routing context and carrier-facing wording leave the app.
    return (f"Shipment: {shipment.reference}\nRoute: {shipment.origin} -> {shipment.destination}\n\n"
            "Please clarify the following for your quotation:\n"
            + "\n".join(f"{index}. {q.question_text}" for index, q in enumerate(question_set.questions, 1))
            + "\n\nThank you.\n")


def _new_draft(repos: Repos, question_set: QuestionSet, quote: Quotation, carrier_id=None) -> ClarificationDraft:
    shipment = repos.get_shipment(str(question_set.shipment_id))
    subject = f"Clarification: {shipment.reference}"
    if quote.quote_reference:
        subject += f" / {quote.quote_reference}"
    subject += f" / quotation {quote.id}"
    return ClarificationDraft(
        shipment_id=shipment.id, quotation_id=quote.id, carrier_id=carrier_id or question_set.carrier_id,
        question_set_id=question_set.id, quotation_version=quote.version,
        recipient=_recipient(repos, quote, carrier_id), subject=subject, body=_body(shipment, question_set),
    )


def _supersede(repos: Repos, quote: Quotation, sets: list[QuestionSet], drafts: list[ClarificationDraft]) -> None:
    for question_set in sets:
        if (question_set.quotation_id == quote.id and question_set.quotation_version != quote.version
                and question_set.status != QuestionSetStatus.superseded):
            updated = question_set.model_copy(update={
                "status": QuestionSetStatus.superseded, "approved_at": None, "updated_at": _now(),
            })
            repos.update_question_set(updated)
            _audit(repos, updated, "superseded", current_quotation_version=quote.version)
    for draft in drafts:
        if (draft.quotation_id == quote.id and draft.quotation_version != quote.version
                and draft.status in {DraftStatus.draft, DraftStatus.approved}):
            updated = draft.model_copy(update={
                "status": DraftStatus.cancelled, "approved_at": None, "updated_at": _now(),
            })
            repos.update_draft(updated)
            _audit(repos, updated, "cancelled_stale", current_quotation_version=quote.version)


def generate_clarification_drafts(settings: Settings, shipment_id: str,
                                  dry_run: bool = False) -> list[ClarificationDraft]:
    """Generate one current question set/draft per quote, including unknown carriers."""
    with with_session(settings) as repos:
        shipment = repos.get_shipment(shipment_id)
        results: list[ClarificationDraft] = []
        agent = None
        with nullcontext() if dry_run else repos.atomic():
            sets = repos.list_question_sets(str(shipment.id))
            drafts = repos.list_drafts(str(shipment.id))
            for quote in repos.list_quotations(str(shipment.id)):
                if not dry_run:
                    _supersede(repos, quote, sets, drafts)
                analysis, pending, resolved = _analysis(repos, shipment, quote)
                if not dry_run:
                    repos.add_gaps(pending)
                    for gap in resolved:
                        repos.update_gap(gap)
                current = next((s for s in reversed(sets) if s.quotation_id == quote.id
                                and s.quotation_version == quote.version
                                and s.status != QuestionSetStatus.superseded), None)
                def signature(questions):
                    return sorted((q.field_name, q.why_it_matters, str(q.priority), q.source_context) for q in questions)
                if current is not None and signature(current.questions) != signature(analysis.clarification_questions):
                    if not dry_run:
                        repos.update_question_set(current.model_copy(update={
                            "status": QuestionSetStatus.superseded, "approved_at": None, "updated_at": _now(),
                        }))
                        _audit(repos, current, "superseded_changed_gaps")
                        for draft in drafts:
                            if draft.question_set_id == current.id and draft.status in {DraftStatus.draft, DraftStatus.approved}:
                                repos.update_draft(draft.model_copy(update={
                                    "status": DraftStatus.cancelled, "approved_at": None, "updated_at": _now(),
                                }))
                                _audit(repos, draft, "cancelled_changed_gaps")
                    current = None
                if current is not None:
                    existing = next((d for d in reversed(drafts) if d.question_set_id == current.id
                                     and d.quotation_version == quote.version
                                     and d.status != DraftStatus.cancelled), None)
                    if existing is not None:
                        results.append(existing)
                        continue
                    if current.questions:
                        draft = _new_draft(repos, current, quote)
                        if not dry_run:
                            repos.add_draft(draft)
                            _audit(repos, draft, "created", question_set_id=str(current.id))
                        results.append(draft)
                    continue
                if analysis.clarification_questions:
                    if agent is None:
                        agent = build_gap_agent(settings)
                    worded = word_questions(agent, shipment, analysis, settings)
                    current = QuestionSet(
                        shipment_id=shipment.id, quotation_id=quote.id, carrier_id=quote.carrier_id,
                        quotation_version=quote.version, questions=worded.clarification_questions,
                        model_name=getattr(agent.model, "model_name", settings.ollama_model),
                        prompt_version=prompts.CLARIFICATION_PROMPT_VERSION,
                    )
                    draft = _new_draft(repos, current, quote)
                    if not dry_run:
                        repos.add_question_set(current)
                        repos.add_draft(draft)
                        _audit(repos, current, "generated", quotation_version=quote.version,
                               model=current.model_name, configured_model=settings.ollama_model,
                               prompt_version=current.prompt_version,
                               system_prompt=prompts.GAP_SYSTEM,
                               wording_changed=worded != analysis)
                        _audit(repos, draft, "created", question_set_id=str(current.id))
                    results.append(draft)
        return results


def create_email_draft(settings: Settings, question_set_id: str,
                       carrier_id: str | None = None) -> ClarificationDraft:
    with with_session(settings) as repos, repos.atomic():
        question_set = repos.get_question_set(question_set_id)
        quote = _current_set(repos, question_set)
        selected = question_set.carrier_id
        if carrier_id is not None:
            carrier = repos.get_carrier(carrier_id)
            if carrier is None:
                raise ValidationFailureError("Selected carrier does not exist.")
            if selected is not None and selected != carrier.id:
                raise ValidationFailureError("Cannot re-target a carrier-specific question set.")
            selected = carrier.id
        if not question_set.questions:
            raise ValidationFailureError("Question set has no material clarification questions.")
        existing = next((d for d in reversed(repos.list_drafts(str(question_set.shipment_id)))
                         if d.question_set_id == question_set.id and d.status != DraftStatus.cancelled), None)
        if existing:
            if selected is not None and existing.carrier_id != selected:
                if existing.carrier_id is not None:
                    raise ValidationFailureError("Cannot re-target an existing carrier-specific draft.")
                existing = existing.model_copy(update={
                    "carrier_id": selected, "recipient": _recipient(repos, quote, selected),
                    "status": DraftStatus.draft, "approved_at": None, "updated_at": _now(),
                })
                repos.update_draft(existing)
                _invalidate_set(repos, question_set)
                _audit(repos, existing, "carrier_assigned", carrier_id=str(selected))
            return existing
        draft = _new_draft(repos, question_set, quote, selected)
        repos.add_draft(draft)
        _audit(repos, draft, "created", question_set_id=str(question_set.id))
        return draft


def _invalidate_set(repos: Repos, question_set: QuestionSet) -> QuestionSet:
    updated = question_set.model_copy(update={
        "status": QuestionSetStatus.draft, "approved_at": None, "updated_at": _now(),
    })
    repos.update_question_set(updated)
    return updated


def edit_question(settings: Settings, question_set_id: str, index: int, text: str) -> QuestionSet:
    if not text.strip():
        raise ValidationFailureError("Question text must not be empty.")
    with with_session(settings) as repos, repos.atomic():
        question_set = repos.get_question_set(question_set_id)
        _current_set(repos, question_set)
        if index < 1 or index > len(question_set.questions):
            raise ValidationFailureError("Question index is 1-based and must identify an existing question.")
        questions = list(question_set.questions)
        questions[index - 1] = questions[index - 1].model_copy(update={"question_text": text.strip()})
        question_set = _invalidate_set(repos, question_set.model_copy(update={"questions": questions}))
        shipment = repos.get_shipment(str(question_set.shipment_id))
        for draft in repos.list_drafts(str(shipment.id)):
            if draft.question_set_id == question_set.id and draft.status != DraftStatus.cancelled:
                updated = draft.model_copy(update={
                    "body": _body(shipment, question_set), "status": DraftStatus.draft,
                    "approved_at": None, "updated_at": _now(),
                })
                repos.update_draft(updated)
                _audit(repos, updated, "question_edit_invalidated_approval", question_index=index)
        _audit(repos, question_set, "question_edited", question_index=index)
        return question_set


def edit_draft(settings: Settings, draft_id: str, subject: str | None = None,
               body: str | None = None, recipient: str | None = None) -> ClarificationDraft:
    with with_session(settings) as repos, repos.atomic():
        draft = repos.get_draft(draft_id)
        question_set, _ = _current_draft(repos, draft)
        changes = {key: value for key, value in {"subject": subject, "body": body, "recipient": recipient}.items()
                   if value is not None}
        changes.update(status=DraftStatus.draft, approved_at=None, updated_at=_now())
        draft = draft.model_copy(update=changes)
        repos.update_draft(draft)
        _invalidate_set(repos, question_set)
        _audit(repos, draft, "edited_approval_invalidated", fields=[k for k in changes if k not in {"status", "approved_at", "updated_at"}])
        return draft


def approve_draft(settings: Settings, draft_id: str) -> ClarificationDraft:
    with with_session(settings) as repos, repos.atomic():
        draft = repos.get_draft(draft_id)
        question_set, _ = _current_draft(repos, draft)
        if not draft.subject.strip() or not draft.body.strip() or not question_set.questions:
            raise ValidationFailureError("Draft subject, body, and questions must be nonempty before approval.")
        _header(draft.subject, "Subject")
        stamp = _now()
        draft = draft.model_copy(update={
            "status": DraftStatus.approved, "approved_at": stamp, "updated_at": stamp,
        })
        question_set = question_set.model_copy(update={
            "status": QuestionSetStatus.approved, "approved_at": stamp, "updated_at": stamp,
        })
        repos.update_question_set(question_set)
        repos.update_draft(draft)
        _audit(repos, question_set, "approved", draft_id=str(draft.id))
        _audit(repos, draft, "approved", quotation_version=draft.quotation_version)
        return draft


def export_draft(settings: Settings, draft_id: str, format: str = "eml",
                 output_path: Path | None = None, recipient: str | None = None) -> Path:
    if format not in {"eml", "txt"}:
        raise ValidationFailureError("Export format must be eml or txt.")
    written: Path | None = None
    try:
        with with_session(settings) as repos, repos.atomic():
            draft = repos.get_draft(draft_id)
            question_set, _ = _current_draft(repos, draft)
            if (draft.status not in {DraftStatus.approved, DraftStatus.exported, DraftStatus.sent_external}
                    or draft.approved_at is None or question_set.status != QuestionSetStatus.approved
                    or question_set.approved_at is None):
                raise ValidationFailureError("Human approval of the current draft and question set is required before export.")
            if not draft.subject.strip() or not draft.body.strip():
                raise ValidationFailureError("Draft subject and body must be nonempty.")
            _header(draft.subject, "Subject")
            chosen = recipient if recipient is not None else draft.recipient
            email = _email(chosen) if chosen else None
            if format == "eml" and email is None:
                raise ValidationFailureError("No recipient is known; supply a single recipient email address for eml export.")
            if format == "eml":
                message = EmailMessage(policy=SMTP)
                message["To"] = email
                message["Subject"] = draft.subject
                message["X-Freight-Shipment-ID"] = str(draft.shipment_id)
                message["X-Freight-Quotation-ID"] = str(draft.quotation_id)
                message.set_content(draft.body)
                data = message.as_bytes()
            else:
                data = (f"To: {email or '(recipient not assigned)'}\nSubject: {draft.subject}\n"
                        f"X-Freight-Shipment-ID: {draft.shipment_id}\n"
                        f"X-Freight-Quotation-ID: {draft.quotation_id}\n\n{draft.body}").encode("utf-8")
            if output_path is None:
                directory = settings.workspace_dir.absolute() / "exports"
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                output = directory / f"clarification-{draft.id}.{format}"
            else:
                output = Path(output_path).absolute()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(output, flags, 0o600)
            written = output
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            updated = draft.model_copy(update={"status": DraftStatus.exported, "updated_at": _now()})
            repos.update_draft(updated)
            _audit(repos, updated, "exported", format=format, path=str(output),
                   recipient=email, quotation_version=draft.quotation_version)
        return output
    except (OSError, SQLAlchemyError, DatabaseError) as exc:
        if written is not None:
            written.unlink(missing_ok=True)
        raise ValidationFailureError(f"Could not export draft without overwriting an existing file: {exc}") from exc

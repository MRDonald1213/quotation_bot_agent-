from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import parseaddr, getaddresses
import json
import logging
import re
from uuid import uuid4

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from ..config import Settings
from ..db.engine import get_session
from ..db.models import AuditEventRow, GmailAccountRow, SupplierEmailDraftRow, utcnow_iso
from ..domain.exceptions import ConfigurationError, NotFoundError, ValidationFailureError

logger = logging.getLogger("freight_agent.gmail_drafts")


def _credentials(settings: Settings) -> Credentials:
    from .gmail_sync import _read_credentials

    return _read_credentials(settings)


def _gmail_draft_message(recipient: str, cc: str, subject: str, body: str, thread_id: str | None = None) -> dict:
    message = EmailMessage()
    message["To"] = recipient
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    message.set_content(body)
    payload: dict = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
    if thread_id:
        payload["threadId"] = thread_id
    return payload


def _create_or_update_gmail_draft(settings: Settings, recipient: str, cc: str, subject: str, body: str,
                                   thread_id: str | None, gmail_draft_id: str | None = None) -> str | None:
    return None  # Draft preparation is local; only explicit SEND contacts Gmail.


def _address(value: str | None) -> str:
    _, address = parseaddr(str(value or ""))
    return (address or "").strip().lower()


def _validate_content(recipient: str, cc: str, subject: str, body: str) -> None:
    recipients = getaddresses([recipient])
    copied = getaddresses([cc]) if cc.strip() else []
    if (len(recipients) != 1 or any("\n" in value or "\r" in value for value in (recipient, cc, subject))
            or not all(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address) for _, address in recipients + copied)
            or not subject.strip() or not body.strip()):
        raise ValidationFailureError("Verify one recipient, valid copied addresses, subject and email content")


def assert_current(session, draft: SupplierEmailDraftRow) -> None:
    from ..db.models import QuotationRow, ClarificationTurnRow
    if draft.quotation_id:
        quotation = session.get(QuotationRow, draft.quotation_id)
        turn = session.get(ClarificationTurnRow, draft.clarification_turn_id) if draft.clarification_turn_id else None
        if not quotation or quotation.disposition or quotation.review_status == "rejected":
            raise ValidationFailureError("Archived quotations cannot send or edit clarification emails")
        if turn and (turn.quotation_version != quotation.version or turn.reply_text is not None):
            raise ValidationFailureError("This email belongs to an answered or older quotation version; create current clarification questions")


def _draft_resolution_status(draft_type: str, draft_status: str, result: dict,
                             requested_fields: list[str] | None = None) -> str | None:
    """Derive message progress from the latest proposal without changing send lifecycle state."""
    unresolved_fields = set(result.get("missing") or []) | set(result.get("uncertain") or [])
    if requested_fields:
        unresolved = unresolved_fields.intersection(requested_fields)
    else:
        # Legacy messages did not retain their field list. Stay conservative: only
        # show them as solved once every current proposal gap has been resolved.
        unresolved = unresolved_fields
    has_reply = bool(result.get("reply_history"))
    if draft_status == "sent":
        if not unresolved:
            return "solved"
        return "follow_up_required" if has_reply else "awaiting_reply"
    if has_reply and unresolved:
        return "follow_up_required"
    return None


def _draft_context(session, settings: Settings, drafts: list[SupplierEmailDraftRow]) -> dict[str, dict]:
    """A draft is reviewed beside the quotation it answers: the human title, the supplier record it
    matched, the email it came from, the offer as it now stands, and the points still open. The page
    must not have to chase those links itself."""
    from ..db.models import (DestinationProposalRow, GmailMessageRow, IntakeItemRow, QuotationRow,
                             SupplierDatabaseRow, SupplierQuotationActionRow)
    from .gmail_sync import _message_quotation_view
    from .quotation_naming import name_for_intake

    empty = {"name": None, "intake_id": None, "message_id": None, "supplier": None,
             "email_subject": None, "email_sent_at": None, "quotation": None,
             "missing_fields": [], "resolution_status": None}
    context = {d.id: {**empty, "name": d.subject} for d in drafts}
    proposal_ids = {d.destination_proposal_id for d in drafts if d.destination_proposal_id}
    if proposal_ids:
        proposals = {row.id: row for row in session.query(DestinationProposalRow).filter(
            DestinationProposalRow.id.in_(proposal_ids)).all()}
        proposal_intake_ids = {row.intake_id for row in proposals.values()}
        proposal_intakes = {row.id: row for row in session.query(IntakeItemRow).filter(
            IntakeItemRow.id.in_(proposal_intake_ids)).all()} if proposal_intake_ids else {}
        proposal_message_ids = {row.gmail_message_id for row in proposal_intakes.values() if row.gmail_message_id}
        proposal_messages = {row.id: row for row in session.query(GmailMessageRow).filter(
            GmailMessageRow.id.in_(proposal_message_ids)).all()} if proposal_message_ids else {}
        from .quotation_naming import name_for_intake
        for draft in drafts:
            proposal = proposals.get(draft.destination_proposal_id or "")
            intake = proposal_intakes.get(proposal.intake_id) if proposal else None
            if proposal is None or intake is None:
                continue
            message = proposal_messages.get(intake.gmail_message_id or "")
            try:
                result = json.loads(proposal.result_json or "{}")
            except (TypeError, ValueError):
                result = {}
            try:
                requested_fields = json.loads(draft.requested_fields_json or "[]")
                if not isinstance(requested_fields, list):
                    requested_fields = []
            except (TypeError, ValueError):
                requested_fields = []
            values = result.get("values", {}) or {}
            supplier = str(values.get("Supplier_Name") or (message.sender if message else "") or "Supplier")
            reference = str(values.get("Quotation_Reference") or values.get("Quotation_ID") or "")
            label = name_for_intake(session, intake.id) or intake.original_filename
            context[draft.id] = {
                **empty,
                "name": f"{supplier} · {reference}" if reference else f"{supplier} · {label}",
                "intake_id": intake.id,
                "message_id": message.id if message else None,
                "supplier": supplier,
                "email_subject": message.subject if message else None,
                "email_sent_at": message.sent_at if message else None,
                "resolution_status": _draft_resolution_status(
                    draft.draft_type, draft.status, result, requested_fields),
                "missing_fields": [{"field": name,
                                    "label": next((str(f.get("label") or name.replace("_", " "))
                                                   for f in json.loads(proposal.schema_json or "[]")
                                                   if f.get("name") == name), name.replace("_", " ")),
                                    "reason": str((result.get("explanations") or {}).get(name) or
                                                  ("Needs supplier confirmation" if name in (result.get("uncertain") or []) else "Missing from quotation"))}
                                   for name in [*(result.get("uncertain") or []), *(result.get("missing") or [])]],
            }
    action_ids = {d.supplier_action_id for d in drafts if d.supplier_action_id}
    if not action_ids:
        return context
    actions = {row.id: row for row in
               session.query(SupplierQuotationActionRow).filter(
                   SupplierQuotationActionRow.id.in_(action_ids)).all()}
    intake_ids = {a.intake_id for a in actions.values() if a.intake_id}
    intakes = {row.id: row for row in session.query(IntakeItemRow).filter(
        IntakeItemRow.id.in_(intake_ids)).all()} if intake_ids else {}
    message_ids = {i.gmail_message_id for i in intakes.values() if i.gmail_message_id}
    messages = {row.id: row for row in session.query(GmailMessageRow).filter(
        GmailMessageRow.id.in_(message_ids)).all()} if message_ids else {}
    database_ids = {a.database_id for a in actions.values() if a.database_id}
    databases = {row.id: row for row in session.query(SupplierDatabaseRow).filter(
        SupplierDatabaseRow.id.in_(database_ids)).all()} if database_ids else {}
    quotes: dict[str, object] = {}
    if intake_ids:
        for quote in session.query(QuotationRow).filter(QuotationRow.intake_item_id.in_(intake_ids)).all():
            best = quotes.get(quote.intake_item_id)
            if best is None or int(quote.version or 0) >= int(best.version or 0):
                quotes[quote.intake_item_id] = quote

    for draft in drafts:
        action = actions.get(draft.supplier_action_id or "")
        if action is None:
            continue
        intake = intakes.get(action.intake_id)
        message = messages.get(intake.gmail_message_id) if intake and intake.gmail_message_id else None
        database = databases.get(action.database_id) if action.database_id else None
        quote = quotes.get(action.intake_id) if action.intake_id else None
        offer = _message_quotation_view(session, settings, quote)
        try:
            missing = json.loads(action.missing_fields_json or "[]")
        except (TypeError, ValueError):
            missing = []
        from .supplier_workflow import _open_questions
        try:
            questions = _open_questions(session, {"intake_id": action.intake_id, "missing_fields": missing})
        except Exception:  # noqa: BLE001 — the card still shows the text when a re-read fails
            questions = []
        try:
            proposed = json.loads(action.proposed_row_json or "{}")
        except (TypeError, ValueError):
            proposed = {}
        # The draft goes to whoever the offer came from: matched record, the name read from the offer, or the sender.
        from .quotation_naming import sender_label
        supplier = ((offer or {}).get("supplier") or proposed.get("Supplier_Name")
                    or sender_label(message.sender if message else (intake.sender if intake else None))
                    or (database.name if database else None))
        context[draft.id] = {
            "name": name_for_intake(session, action.intake_id) if action.intake_id else draft.subject,
            "intake_id": action.intake_id,
            "message_id": message.id if message else None,
            "supplier": supplier,
            "email_subject": message.subject if message else None,
            "email_sent_at": message.sent_at if message else None,
            "quotation": offer,
            "missing_fields": [{"field": item.get("field"),
                                "label": item.get("label") or str(item.get("field") or "").replace("_", " "),
                                "reason": item.get("reason", "")}
                               for item in questions],
        }
    return context


def list_drafts(settings: Settings) -> list[dict]:
    with get_session(settings) as session:
        rows = session.query(SupplierEmailDraftRow).order_by(SupplierEmailDraftRow.updated_at.desc()).all()
        context = _draft_context(session, settings, list(rows))
    return [{**context.get(row.id, {"name": row.subject}),
             "id": row.id, "quotation_id": row.quotation_id, "clarification_turn_id": row.clarification_turn_id,
             "destination_proposal_id": row.destination_proposal_id, "draft_type": row.draft_type,
             "name": context.get(row.id, {}).get("name") or row.subject,
             "recipient": row.recipient, "bound_recipient": row.bound_recipient,
             "recipient_locked": bool(row.bound_recipient), "cc": row.cc, "subject": row.subject,
             "body": row.body, "status": row.status, "gmail_draft_id": row.gmail_draft_id,
             "gmail_thread_id": row.gmail_thread_id, "supplier_action_id": row.supplier_action_id,
             "sent_message_id": row.sent_message_id, "sent_at": row.sent_at,
             "updated_at": row.updated_at} for row in rows]


def save_draft(settings: Settings, draft_id: str, recipient: str, cc: str, subject: str, body: str) -> dict:
    _validate_content(recipient, cc, subject, body)
    now = utcnow_iso()
    with get_session(settings) as session:
        draft = session.get(SupplierEmailDraftRow, draft_id)
        if draft is None:
            raise NotFoundError("Email draft not found")
        if draft.status in {"sent", "sending"}:
            raise ValidationFailureError("Sent or pending-delivery drafts cannot be edited")
        assert_current(session, draft)
        draft.status = "draft"
        draft.approved_recipient = None
        draft.immutable_approved_body = None
        draft.approved_subject = None
        if draft.bound_recipient and _address(recipient) != _address(draft.bound_recipient):
            raise ValidationFailureError(
                f"This reply can only go to {draft.bound_recipient}, the sender of the quotation email.")
        draft.recipient = recipient.strip()
        draft.cc = cc.strip()
        draft.subject = subject.strip()
        draft.body = body
        draft.updated_at = now
        gmail_draft_id = _create_or_update_gmail_draft(settings, draft.recipient, draft.cc, draft.subject,
                                                        draft.body, draft.gmail_thread_id, draft.gmail_draft_id)
        if gmail_draft_id:
            draft.gmail_draft_id = gmail_draft_id
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_email_draft", entity_id=draft.id,
            action="human_saved", details_json=json.dumps({"draft_type": draft.draft_type,
                "destination_proposal_id": draft.destination_proposal_id}), created_at=now))
        session.commit()
        draft_id = draft.id
    return get_draft(settings, draft_id)


def get_draft(settings: Settings, draft_id: str) -> dict:
    with get_session(settings) as session:
        draft = session.get(SupplierEmailDraftRow, draft_id)
        if draft is None:
            raise NotFoundError("Email draft not found")
        context = _draft_context(session, settings, [draft]).get(draft.id, {})
        return {"id": draft.id, "name": context.get("name") or draft.subject,
                "quotation_id": draft.quotation_id, "clarification_turn_id": draft.clarification_turn_id,
                "destination_proposal_id": draft.destination_proposal_id, "draft_type": draft.draft_type,
                "recipient": draft.recipient, "bound_recipient": draft.bound_recipient,
                "recipient_locked": bool(draft.bound_recipient), "cc": draft.cc, "subject": draft.subject,
                "body": draft.body, "status": draft.status, "gmail_draft_id": draft.gmail_draft_id,
                "gmail_thread_id": draft.gmail_thread_id, "supplier_action_id": draft.supplier_action_id,
                "approved_recipient": draft.approved_recipient,
                "sent_message_id": draft.sent_message_id, "sent_at": draft.sent_at,
                "immutable_approved_body": draft.immutable_approved_body,
                "approved_subject": draft.approved_subject, "updated_at": draft.updated_at,
                "resolution_status": context.get("resolution_status")}


def create_draft(settings: Settings, *, recipient: str, cc: str, subject: str, body: str,
                 gmail_account_id: str, quotation_register_id: str | None = None,
                 supplier_master_id: str | None = None, gmail_thread_id: str | None = None,
                 supplier_action_id: str | None = None, bound_recipient: str | None = None,
                 destination_proposal_id: str | None = None, draft_type: str = "missing_information") -> dict:
    _validate_content(recipient, cc, subject, body)
    if not recipient.strip() or not subject.strip() or not body.strip():
        raise ValidationFailureError("recipient, subject and body are required")
    if not _address(recipient):
        raise ValidationFailureError("The draft recipient is not a single valid email address.")
    now = utcnow_iso()
    with get_session(settings) as session:
        if session.get(GmailAccountRow, gmail_account_id) is None:
            raise NotFoundError("Gmail account not found")
        row = SupplierEmailDraftRow(id=str(uuid4()), quotation_register_id=quotation_register_id,
                                    supplier_master_id=supplier_master_id, gmail_account_id=gmail_account_id,
                                    destination_proposal_id=destination_proposal_id, draft_type=draft_type,
                                    gmail_thread_id=gmail_thread_id, supplier_action_id=supplier_action_id,
                                    bound_recipient=(bound_recipient or "").strip() or None,
                                    recipient=recipient.strip(), cc=cc.strip(),
                                    subject=subject.strip(), body=body, status="draft", created_at=now, updated_at=now)
        gmail_draft_id = _create_or_update_gmail_draft(settings, recipient.strip(), cc.strip(), subject.strip(), body,
                                                        gmail_thread_id)
        if gmail_draft_id:
            row.gmail_draft_id = gmail_draft_id
        session.add(row)
        session.commit()
        draft_id = row.id
    return get_draft(settings, draft_id)


def upsert_action_draft(settings: Settings, *, supplier_action_id: str, recipient: str, subject: str,
                        body: str, gmail_account_id: str, bound_recipient: str | None = None,
                        cc: str = "", gmail_thread_id: str | None = None) -> dict:
    """One draft per supplier analysis: refresh the unsent one instead of stacking duplicates."""
    with get_session(settings) as session:
        rows = session.query(SupplierEmailDraftRow).filter(
            SupplierEmailDraftRow.supplier_action_id == supplier_action_id).all()
        current = [(row.id, row.status) for row in rows]
    open_draft = next((draft_id for draft_id, status in current if status == "draft"), None)
    if open_draft is not None:
        return save_draft(settings, open_draft, recipient=recipient, cc=cc, subject=subject, body=body)
    locked = next((draft_id for draft_id, status in current if status == "approved"), None)
    if locked is not None:
        return get_draft(settings, locked)
    return create_draft(settings, recipient=recipient, cc=cc, subject=subject, body=body,
                        gmail_account_id=gmail_account_id, gmail_thread_id=gmail_thread_id,
                        supplier_action_id=supplier_action_id, bound_recipient=bound_recipient)


def draft_for_action(settings: Settings, supplier_action_id: str) -> dict | None:
    """The draft this analysis produced, newest first — read-only view for the message page."""
    with get_session(settings) as session:
        row = (session.query(SupplierEmailDraftRow)
               .filter(SupplierEmailDraftRow.supplier_action_id == supplier_action_id)
               .order_by(SupplierEmailDraftRow.updated_at.desc()).first())
        if row is None:
            return None
        draft_id = row.id
    return get_draft(settings, draft_id)


def approve_draft(settings: Settings, draft_id: str) -> dict:
    now = utcnow_iso()
    with get_session(settings) as session:
        draft = session.get(SupplierEmailDraftRow, draft_id)
        if draft is None:
            raise NotFoundError("Email draft not found")
        if draft.status in {"sent", "sending"}:
            raise ValidationFailureError("Sent or pending-delivery drafts cannot be approved again")
        if draft.status == "cancelled":
            raise ValidationFailureError("Restore and save a cancelled draft before approval")
        _validate_content(draft.recipient, draft.cc, draft.subject, draft.body)
        assert_current(session, draft)
        if draft.clarification_turn_id:
            from ..db.models import ClarificationTurnRow, ClarificationDraftRow, QuestionSetRow
            turn = session.get(ClarificationTurnRow, draft.clarification_turn_id)
            if turn:
                questions = json.loads(turn.questions_json)
                verified = all(q["text"] in draft.body for q in questions)
                if verified:
                    questions.sort(key=lambda q: draft.body.index(q["text"]))
                for question in questions:
                    question["order_verified"] = verified
                turn.questions_json = json.dumps(questions)
            local = session.get(ClarificationDraftRow, draft.clarification_turn_id)
            if local:
                local.subject, local.body, local.recipient = draft.subject, draft.body, draft.recipient
                local.status, local.approved_at = "approved", now
            question_set = session.get(QuestionSetRow, draft.clarification_turn_id)
            if question_set:
                question_set.status, question_set.approved_at = "approved", now
        if not _address(draft.recipient) or "@" not in _address(draft.recipient) or not draft.subject.strip() or not draft.body.strip():
            raise ValidationFailureError("Verify recipient, subject and body before approval")
        if draft.bound_recipient and _address(draft.recipient) != _address(draft.bound_recipient):
            raise ValidationFailureError(
                f"This reply can only go to {draft.bound_recipient}, the sender of the quotation email.")
        draft.status = "approved"
        draft.approved_recipient = draft.recipient.strip()
        draft.immutable_approved_body = draft.body
        draft.approved_subject = draft.subject
        draft.updated_at = now
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_email_draft", entity_id=draft.id,
                                  action="human_approved", details_json=json.dumps(
                                      {"approved": True, "recipient": draft.approved_recipient}), created_at=now))
        session.commit()
    return get_draft(settings, draft_id)


def discard_draft(settings: Settings, draft_id: str) -> dict:
    """Cancel an unsent draft while retaining its audit history."""
    with get_session(settings) as session:
        draft = session.get(SupplierEmailDraftRow, draft_id)
        if draft is None:
            raise NotFoundError("Email draft not found")
        if draft.status in {"sent", "sending"}:
            raise ValidationFailureError("Sent or pending-delivery drafts cannot be discarded")
        if draft.status != "cancelled":
            now = utcnow_iso()
            draft.status = "cancelled"
            draft.approved_recipient = None
            draft.immutable_approved_body = None
            draft.approved_subject = None
            draft.updated_at = now
            session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_email_draft", entity_id=draft.id,
                action="discarded", details_json=json.dumps({"draft_type": draft.draft_type}), created_at=now))
            session.commit()
    return get_draft(settings, draft_id)

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import parseaddr
import json
import logging
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
    try:
        credentials = _credentials(settings)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        payload = {"message": _gmail_draft_message(recipient, cc, subject, body, thread_id)}
        if gmail_draft_id:
            result = service.users().drafts().update(userId="me", id=gmail_draft_id, body=payload).execute()
        else:
            result = service.users().drafts().create(userId="me", body=payload).execute()
        return result.get("id")
    except Exception as exc:  # noqa: BLE001 — the local draft is still the record of the text
        logger.warning("Gmail-side draft could not be written: %s", type(exc).__name__)
        return None


def _address(value: str | None) -> str:
    _, address = parseaddr(str(value or ""))
    return (address or "").strip().lower()


def list_drafts(settings: Settings) -> list[dict]:
    with get_session(settings) as session:
        rows = session.query(SupplierEmailDraftRow).order_by(SupplierEmailDraftRow.updated_at.desc()).all()
    return [{"id": row.id, "recipient": row.recipient, "bound_recipient": row.bound_recipient,
             "recipient_locked": bool(row.bound_recipient), "cc": row.cc, "subject": row.subject,
             "body": row.body, "status": row.status, "gmail_draft_id": row.gmail_draft_id,
             "gmail_thread_id": row.gmail_thread_id, "supplier_action_id": row.supplier_action_id,
             "sent_message_id": row.sent_message_id, "sent_at": row.sent_at,
             "updated_at": row.updated_at} for row in rows]


def save_draft(settings: Settings, draft_id: str, recipient: str, cc: str, subject: str, body: str) -> dict:
    now = utcnow_iso()
    with get_session(settings) as session:
        draft = session.get(SupplierEmailDraftRow, draft_id)
        if draft is None:
            raise NotFoundError("Email draft not found")
        if draft.status in {"approved", "sent"}:
            raise ValidationFailureError("Approved or sent drafts are locked and cannot be edited")
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
        session.commit()
        draft_id = draft.id
    return get_draft(settings, draft_id)


def get_draft(settings: Settings, draft_id: str) -> dict:
    with get_session(settings) as session:
        draft = session.get(SupplierEmailDraftRow, draft_id)
        if draft is None:
            raise NotFoundError("Email draft not found")
        return {"id": draft.id, "recipient": draft.recipient, "bound_recipient": draft.bound_recipient,
                "recipient_locked": bool(draft.bound_recipient), "cc": draft.cc, "subject": draft.subject,
                "body": draft.body, "status": draft.status, "gmail_draft_id": draft.gmail_draft_id,
                "gmail_thread_id": draft.gmail_thread_id, "supplier_action_id": draft.supplier_action_id,
                "approved_recipient": draft.approved_recipient,
                "sent_message_id": draft.sent_message_id, "sent_at": draft.sent_at,
                "immutable_approved_body": draft.immutable_approved_body,
                "approved_subject": draft.approved_subject, "updated_at": draft.updated_at}


def create_draft(settings: Settings, *, recipient: str, cc: str, subject: str, body: str,
                 gmail_account_id: str, quotation_register_id: str | None = None,
                 supplier_master_id: str | None = None, gmail_thread_id: str | None = None,
                 supplier_action_id: str | None = None, bound_recipient: str | None = None) -> dict:
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
    locked = next((draft_id for draft_id, status in current if status in {"approved", "sent"}), None)
    if locked is not None:
        return get_draft(settings, locked)
    return create_draft(settings, recipient=recipient, cc=cc, subject=subject, body=body,
                        gmail_account_id=gmail_account_id, gmail_thread_id=gmail_thread_id,
                        supplier_action_id=supplier_action_id, bound_recipient=bound_recipient)


def approve_draft(settings: Settings, draft_id: str) -> dict:
    now = utcnow_iso()
    with get_session(settings) as session:
        draft = session.get(SupplierEmailDraftRow, draft_id)
        if draft is None:
            raise NotFoundError("Email draft not found")
        if draft.status == "sent":
            raise ValidationFailureError("Sent drafts cannot be approved again")
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

from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from uuid import uuid4

from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from ..config import Settings
from ..db.engine import get_session
from ..db.models import AuditEventRow, SupplierEmailDraftRow, utcnow_iso
from ..domain.exceptions import ConfigurationError, NotFoundError, ValidationFailureError
from .gmail_sync import _read_credentials


def _mime_message(draft: SupplierEmailDraftRow) -> bytes:
    message = EmailMessage()
    message["To"] = draft.recipient
    if draft.cc:
        message["Cc"] = draft.cc
    message["Subject"] = draft.approved_subject or draft.subject
    message.set_content(draft.immutable_approved_body or draft.body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def send_approved_draft(settings: Settings, draft_id: str, confirmation_text: str, idempotency_key: str) -> dict:
    if confirmation_text.strip().upper() != "SEND":
        raise ValidationFailureError("Send confirmation must equal SEND")
    if not idempotency_key.strip():
        raise ValidationFailureError("An idempotency key is required")

    credentials = _read_credentials(settings)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    now = utcnow_iso()
    with get_session(settings) as session:
        draft = session.get(SupplierEmailDraftRow, draft_id)
        if draft is None:
            raise NotFoundError("Email draft not found")
        if draft.status == "sent":
            if draft.sent_message_id:
                return {"status": "sent", "sent_message_id": draft.sent_message_id, "idempotent": True}
            raise ValidationFailureError("Draft is marked sent but has no sent message ID")
        if draft.status != "approved":
            raise ValidationFailureError("Only a human-approved draft may be sent")
        if not draft.immutable_approved_body or not draft.approved_subject:
            raise ValidationFailureError("Approved draft content is incomplete")
        if draft.idempotency_key and draft.idempotency_key != idempotency_key.strip():
            raise ValidationFailureError("This draft has a different idempotency key")

        raw = _mime_message(draft)
        body = {"raw": raw}
        if draft.gmail_thread_id:
            body["threadId"] = draft.gmail_thread_id
        created = service.users().drafts().create(userId="me", body={"message": body}).execute()
        gmail_draft_id = created.get("id")
        if not gmail_draft_id:
            raise ValidationFailureError("Gmail did not return a draft ID for the approved content")
        sent = service.users().drafts().send(userId="me", body={"id": gmail_draft_id}).execute()
        sent_message_id = sent.get("id")
        if not sent_message_id:
            raise ValidationFailureError("Gmail did not confirm the sent message ID")

        draft.gmail_draft_id = gmail_draft_id
        draft.status = "sent"
        draft.sent_message_id = sent_message_id
        draft.sent_at = now
        draft.idempotency_key = idempotency_key.strip()
        draft.updated_at = now
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_email_draft", entity_id=draft.id,
                                  action="human_sent", details_json=json.dumps({"sent_message_id": sent_message_id, "idempotency_key": idempotency_key.strip()}), created_at=now))
        session.commit()
    return {"status": "sent", "sent_message_id": sent_message_id, "idempotent": False}

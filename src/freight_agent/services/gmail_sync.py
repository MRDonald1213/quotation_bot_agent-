from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import re
from email.utils import parseaddr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from ..config import Settings
from ..db.engine import get_session
from ..db.models import GmailAccountRow, GmailAttachmentRow, GmailMessageRow, utcnow_iso
from ..domain.exceptions import ConfigurationError, ValidationFailureError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose"]
logger = logging.getLogger("freight_agent.gmail_sync")
_SYNC_LOCK = threading.Lock()
_SYNC_STATUS = {
    "automatic": False,
    "running": False,
    "last_successful_sync_at": None,
    "next_sync_at": None,
    "new_messages": 0,
    "quotation_messages": 0,
    "ignored_messages": 0,
    "error": None,
}


def _credential_path(settings: Settings) -> Path:
    if not settings.google_oauth_credentials_path:
        raise ConfigurationError("Gmail OAuth is not configured: set GOOGLE_OAUTH_CREDENTIALS_PATH in .env")
    path = settings.project_credentials_path
    if not path.is_file():
        raise ConfigurationError(f"Gmail OAuth credentials file not found: {path}")
    return path


def _token_path(settings: Settings) -> Path:
    path = settings.project_oauth_token_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def gmail_status(settings: Settings) -> dict:
    with get_session(settings) as session:
        accounts = session.query(GmailAccountRow).order_by(GmailAccountRow.updated_at.desc()).all()
    return {
        "configured": bool(settings.google_oauth_credentials_path),
        "accounts": [{"id": row.id, "email": row.email, "status": row.status, "last_synced_at": row.last_synced_at} for row in accounts],
        "send_enabled": True,
        "warning": "Email sending is only permitted through an explicit human-confirmed Draft Review action.",
        **_SYNC_STATUS,
        "automatic_status": "running" if _SYNC_STATUS["automatic"] else "stopped",
    }


def _classify_supplier_message(subject: str, body: str, attachments: list[str]) -> tuple[str, float, str]:
    text = " ".join([subject, body, *attachments]).lower()
    quotation_terms = ("quotation", "quote", "offer", "price", "pricing", "freight rate", "tariff", "incoterm", "valid until")
    reply_terms = ("re:", "clarification", "regarding your quotation", "our quotation", "in response to")
    attachment_ext = any(name.lower().endswith((".pdf", ".xlsx", ".xls", ".doc", ".docx", ".csv", ".txt", ".eml")) for name in attachments)
    matches = sum(term in text for term in quotation_terms)
    if any(term in text for term in reply_terms) and subject.lower().startswith(("re:", "clarification", "fw:")):
        return "SUPPLIER_QUOTATION_REPLY", 0.82, "Detected a supplier reply to an existing quotation conversation"
    if matches >= 2 or attachment_ext and matches >= 1:
        return "NEW_QUOTATION", 0.82 if matches >= 3 else 0.68, "Detected quotation language or a quotation document attachment"
    if attachment_ext:
        return "UNCERTAIN", 0.55, "Supported attachment found, but quotation evidence is not confirmed"
    return "NON_QUOTATION", 0.8, "No reliable quotation evidence found"


def _sender_address(sender: str) -> str | None:
    _, address = parseaddr(sender)
    if not address:
        return None
    address = address.strip()
    return address if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address) else None


def gmail_authorization_url(settings: Settings, redirect_uri: str, code_verifier: str) -> dict:
    credentials = _credential_path(settings)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    flow = Flow.from_client_secrets_file(str(credentials), scopes=SCOPES, redirect_uri=redirect_uri)
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    return {"authorization_url": authorization_url, "state": state}


def gmail_oauth_callback(settings: Settings, code: str, redirect_uri: str, code_verifier: str) -> dict:
    credentials = _credential_path(settings)
    flow = Flow.from_client_secrets_file(str(credentials), scopes=SCOPES, redirect_uri=redirect_uri)
    try:
        flow.fetch_token(code=code, code_verifier=code_verifier)
    except Exception:
        raise ValidationFailureError("Gmail authorization failed or expired. Please start the connection again.")
    credential = flow.credentials

    try:
        service = build("gmail", "v1", credentials=credential, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
    except Exception as exc:
        raise ValidationFailureError("Gmail authorization succeeded but account verification failed. Please try again.") from exc
    email = profile.get("emailAddress", "")
    if not email:
        raise ValidationFailureError("Gmail authorization did not return an account email address. Please try again.")

    token_path = _token_path(settings)
    token_path.write_text(credential.to_json(), encoding="utf-8")
    os.chmod(token_path, 0o600)
    now = utcnow_iso()
    with get_session(settings) as session:
        existing = session.query(GmailAccountRow).filter(GmailAccountRow.email == email).first()
        if existing:
            account = existing
            account.token_path = str(token_path)
            account.scopes_json = json.dumps(SCOPES)
            account.status = "connected"
            account.updated_at = now
            account_id = account.id
        else:
            account_id = str(uuid4())
            session.add(GmailAccountRow(id=account_id, email=email, token_path=str(token_path), scopes_json=json.dumps(SCOPES), status="connected", created_at=now, updated_at=now))
        session.commit()
    return {"account_id": account_id, "email": email, "status": "connected"}


def _read_credentials(settings: Settings) -> Credentials:
    token_path = _token_path(settings)
    if not token_path.is_file():
        raise ConfigurationError("Gmail is not connected. Complete OAuth authorization first.")
    return Credentials.from_authorized_user_file(str(token_path), SCOPES)


def _header(headers: list[dict], name: str) -> str:
    target = name.lower()
    return next((str(item.get("value", "")) for item in headers if str(item.get("name", "")).lower() == target), "")


def _body(payload: dict) -> str:
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", errors="replace")
    parts = payload.get("parts", []) or []
    plain = []
    for part in parts:
        mime = part.get("mimeType", "")
        if mime == "text/plain" and part.get("body", {}).get("data"):
            plain.append(base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", errors="replace"))
    if plain:
        return "\n".join(plain)
    for part in parts:
        child = _body(part)
        if child:
            return child
    return ""


def _attachment_parts(payload: dict) -> list[dict]:
    found: list[dict] = []
    if payload.get("filename"):
        found.append(payload)
    for child in payload.get("parts", []) or []:
        found.extend(_attachment_parts(child))
    return found


def _process_quotation_message(settings: Settings, message_id: str, text_content: str, sender: str, subject: str, attachment_paths: list[Path]) -> str | None:
    from ..db.models import IntakeItemRow, SupplierDatabaseRow, utcnow_iso
    from ..parsers.base import parse_file

    parsed_text = text_content.strip()
    evidence_path = attachment_paths[0] if attachment_paths else None
    for attachment in attachment_paths:
        if attachment.suffix.lower() in {".pdf", ".xlsx", ".xls", ".docx", ".txt", ".eml", ".csv"}:
            try:
                parsed = parse_file(attachment)
                parsed_text += "\n" + parsed.text
                evidence_path = evidence_path or attachment
            except Exception:
                continue
    if not parsed_text.strip():
        return None

    now = utcnow_iso()
    filename = evidence_path.name if evidence_path else f"gmail-{message_id[:8]}-quotation.txt"
    stored_path = evidence_path if evidence_path else settings.workspace_dir.resolve() / "gmail_quotations" / filename
    if evidence_path is None:
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        stored_path.write_text(parsed_text, encoding="utf-8")
    digest = hashlib.sha256(parsed_text.encode("utf-8")).hexdigest()
    with get_session(settings) as session:
        existing = session.query(IntakeItemRow).filter(IntakeItemRow.content_sha256 == digest).first()
        if existing:
            intake_id = existing.id
            if (existing.source_type in {"gmail_body", "gmail_attachment"}
                    and existing.classification_status == "quotation"):
                existing.classification_status = "pending"
                existing.classification_reason = "Gmail quotation waiting for classification"
                existing.classification_confidence = None
                existing.classification_json = None
                existing.processing_status = "pending"
            if existing.gmail_message_id is None:
                existing.gmail_message_id = message_id
            session.commit()
        else:
            row = IntakeItemRow(id=str(uuid4()), shipment_id=None, source_type="gmail_attachment" if evidence_path else "gmail_body",
                                original_filename=filename, stored_path=str(stored_path.resolve()), content_sha256=digest,
                                sender=sender, subject=subject, received_at=now, raw_text=parsed_text,
                                file_size_bytes=stored_path.stat().st_size if stored_path.is_file() else len(parsed_text.encode()),
                                classification_status="pending", classification_reason="Gmail quotation waiting for classification",
                                classification_confidence=None, classification_json=None, processing_status="pending",
                                gmail_message_id=message_id, created_at=now)
            session.add(row)
            session.flush()
            intake_id = row.id
            session.commit()
    return intake_id


def analyze_inbound_quotation(settings: Settings, message_row_id: str, intake_id: str) -> str:
    """Run the whole inbound-quote path: extract, match the supplier database, reveal gaps, draft the reply.

    Stopping at the draft is deliberate — a person still approves and sends it.
    """
    from .supplier_workflow import run_quotation_pipeline

    try:
        outcome = run_quotation_pipeline(settings, intake_id)
    except Exception as exc:  # noqa: BLE001 — one bad email must not stop the mailbox
        logger.warning("automatic analysis failed for %s: %s", intake_id, type(exc).__name__, exc_info=True)
        status = "extraction_failed"
    else:
        action = outcome["action"]
        if outcome.get("draft"):
            status = "draft_ready"
        elif not action.get("database_id"):
            status = "needs_database"
        elif action["status"] == "ready_for_approval":
            status = "ready_for_approval"
        elif action["status"] == "needs_clarification":
            status = "needs_clarification"
        else:
            status = "needs_human_review"
    with get_session(settings) as session:
        row = session.get(GmailMessageRow, message_row_id)
        if row is not None:
            row.processing_status = status
            session.commit()
    return status


def ensure_message_quotation(settings: Settings, message_row_id: str) -> str | None:
    with get_session(settings) as session:
        message = session.get(GmailMessageRow, message_row_id)
        if message is None:
            raise ValidationFailureError("Gmail message not found")
        if message.classification not in {"NEW_QUOTATION", "SUPPLIER_QUOTATION_REPLY", "UNCERTAIN"}:
            return None
        attachments = session.query(GmailAttachmentRow).filter(GmailAttachmentRow.message_id == message_row_id).all()
        attachment_paths = [Path(attachment.stored_path) for attachment in attachments if attachment.stored_path and Path(attachment.stored_path).is_file()]
        text_content = "\n".join(part for part in (message.subject, message.body_text) if part)
    return _process_quotation_message(settings, message_row_id, text_content, message.sender, message.subject, attachment_paths)


def gmail_message_detail(settings: Settings, message_row_id: str) -> dict:
    intake_id = ensure_message_quotation(settings, message_row_id)
    with get_session(settings) as session:
        message = session.get(GmailMessageRow, message_row_id)
        if message is None:
            raise ValidationFailureError("Gmail message not found")
        attachments = session.query(GmailAttachmentRow).filter(GmailAttachmentRow.message_id == message_row_id).order_by(GmailAttachmentRow.created_at).all()
    return {
        "id": message.id,
        "gmail_message_id": message.gmail_message_id,
        "gmail_thread_id": message.gmail_thread_id,
        "sender": message.sender,
        "subject": message.subject,
        "body_text": message.body_text,
        "classification": message.classification,
        "processing_status": message.processing_status,
        "sent_at": message.sent_at,
        "created_at": message.created_at,
        "intake_id": intake_id,
        "attachments": [{"id": attachment.id, "filename": attachment.filename, "mime_type": attachment.mime_type,
                         "size_bytes": attachment.size_bytes, "stored_path": attachment.stored_path,
                         "created_at": attachment.created_at} for attachment in attachments],
    }


def gmail_sync(settings: Settings, limit: int = 100, backfill_days: int | None = None) -> dict:
    if not _SYNC_LOCK.acquire(blocking=False):
        return {**_SYNC_STATUS, "status": "sync_already_running", "imported": 0, "discovered": 0}
    _SYNC_STATUS["running"] = True
    try:
        credentials = _read_credentials(settings)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        with get_session(settings) as session:
            account = session.query(GmailAccountRow).filter(GmailAccountRow.status == "connected").first()
            if account is None:
                raise ConfigurationError("No connected Gmail account found")
            account_id = account.id

        account_history_id = None
        with get_session(settings) as session:
            account_row = session.get(GmailAccountRow, account_id)
            account_history_id = account_row.history_id

        window_days = backfill_days if backfill_days is not None else settings.gmail_backfill_days
        query = f"newer_than:{window_days}d" if not account_history_id else None
        request = service.users().messages().list(userId="me", maxResults=max(1, min(limit, 100)), labelIds=["INBOX"])
        if query:
            request = service.users().messages().list(userId="me", maxResults=max(1, min(limit, 100)), labelIds=["INBOX"], q=query)
        response = request.execute()
        imported = 0
        quotation_count = 0
        ignored_count = 0
        discovered = len(response.get("messages", []))
        now = utcnow_iso()
        attachment_dir = settings.workspace_dir.resolve() / "gmail_attachments"
        attachment_dir.mkdir(parents=True, exist_ok=True)

        for summary in response.get("messages", []):
            gmail_message_id = summary["id"]
            with get_session(settings) as session:
                if session.query(GmailMessageRow).filter(GmailMessageRow.gmail_message_id == gmail_message_id).first():
                    continue
            full = service.users().messages().get(userId="me", id=gmail_message_id, format="full").execute()
            headers = full.get("payload", {}).get("headers", [])
            body_text = _body(full.get("payload", {}))
            sender = _header(headers, "from")
            recipients_json = json.dumps([value for value in (_header(headers, "to"), _header(headers, "cc")) if value])
            subject = _header(headers, "subject")
            attachment_paths: list[Path] = []
            attachment_names: list[str] = []
            sent_at = utcnow_iso() if full.get("internalDate") else None

            for part in _attachment_parts(full.get("payload", {})):
                filename = part.get("filename")
                attachment_id = part.get("body", {}).get("attachmentId")
                if not filename or not attachment_id:
                    continue
                attachment = service.users().messages().attachments().get(userId="me", messageId=gmail_message_id, id=attachment_id).execute()
                data = attachment.get("data")
                if not data:
                    continue
                bytes_value = base64.urlsafe_b64decode(data + "==")
                stored_hash = hashlib.sha256(bytes_value).hexdigest()
                safe_name = Path(filename).name
                stored_path = attachment_dir / f"{stored_hash}-{safe_name}"
                stored_path.write_bytes(bytes_value)
                attachment_paths.append(stored_path)
                attachment_names.append(safe_name)

            classification, confidence, reason = _classify_supplier_message(subject, body_text, attachment_names)
            source_hash = hashlib.sha256((sender + "\n" + subject + "\n" + body_text).encode("utf-8")).hexdigest()
            row_id = str(uuid4())
            with get_session(settings) as session:
                message = GmailMessageRow(id=row_id, gmail_message_id=gmail_message_id,
                    gmail_thread_id=full.get("threadId"), account_id=account_id,
                    sender=sender, recipients_json=recipients_json, subject=subject,
                    body_text=body_text if classification != "NON_QUOTATION" else "",
                    sent_at=sent_at, classification=classification,
                    processing_status="received" if classification != "NON_QUOTATION" else "ignored",
                    source_hash=source_hash, created_at=now)
                session.add(message)
                session.flush()
                for stored_path, attachment_name in zip(attachment_paths, attachment_names):
                    stored_hash = hashlib.sha256(stored_path.read_bytes()).hexdigest()
                    session.add(GmailAttachmentRow(id=str(uuid4()), message_id=row_id,
                        gmail_attachment_id=next((p.get("body", {}).get("attachmentId", "") for p in (full.get("payload", {}).get("parts", []) or []) if p.get("filename") == attachment_name), ""),
                        filename=attachment_name, mime_type="application/octet-stream", size_bytes=stored_path.stat().st_size,
                        source_hash=stored_hash, stored_path=str(stored_path.resolve()), created_at=now))
                session.commit()
            imported += 1
            if classification == "NEW_QUOTATION":
                quotation_count += 1
                message_text = "\n".join([subject, body_text])
                intake_id = _process_quotation_message(settings, row_id, message_text, sender, subject, attachment_paths)
                if intake_id:
                    # a quotation that arrives by email is analyzed and drafted without another click
                    analyze_inbound_quotation(settings, row_id, intake_id)
                with get_session(settings) as session:
                    row = session.get(GmailMessageRow, row_id)
                    row.processing_status = "awaiting_supplier_review" if row.processing_status == "received" else row.processing_status
                    session.commit()
            elif classification == "SUPPLIER_QUOTATION_REPLY":
                quotation_count += 1
                with get_session(settings) as session:
                    row = session.get(GmailMessageRow, row_id)
                    row.processing_status = "supplier_reply_pending"
                    session.commit()
            else:
                ignored_count += 1
                # Keep only synchronization identity and minimum state for unrelated mail; body was deliberately omitted above.

        profile = service.users().getProfile(userId="me").execute()
        with get_session(settings) as session:
            account_row = session.get(GmailAccountRow, account_id)
            account_row.last_synced_at = now
            account_row.history_id = str(profile.get("historyId") or account_row.history_id or "") or None
            session.commit()
        _SYNC_STATUS.update({"running": False, "last_successful_sync_at": now, "new_messages": imported,
                             "quotation_messages": quotation_count, "ignored_messages": ignored_count,
                             "error": None})
        if discovered == 0:
            _SYNC_STATUS["error"] = None
        return {"status": "succeeded", "imported": imported, "discovered": discovered,
                "quotation_messages": quotation_count, "ignored_messages": ignored_count,
                "message": "No new quotation emails found" if quotation_count == 0 else f"{quotation_count} quotation email(s) found"}
    except Exception as exc:
        _SYNC_STATUS["running"] = False
        _SYNC_STATUS["error"] = str(exc)[:500]
        raise
    finally:
        _SYNC_LOCK.release()

def gmail_inbox(settings: Settings, limit: int = 100) -> list[dict]:
    with get_session(settings) as session:
        rows = session.query(GmailMessageRow).filter(GmailMessageRow.classification.in_(["NEW_QUOTATION", "SUPPLIER_QUOTATION_REPLY", "UNCERTAIN"])).order_by(GmailMessageRow.created_at.desc()).limit(max(1, min(limit, 100))).all()
        attachments = session.query(GmailAttachmentRow).filter(GmailAttachmentRow.message_id.in_([row.id for row in rows])).all() if rows else []
    counts: dict[str, int] = {}
    names: dict[str, list[str]] = {}
    for attachment in attachments:
        counts[attachment.message_id] = counts.get(attachment.message_id, 0) + 1
        names.setdefault(attachment.message_id, []).append(attachment.filename)
    return [{"id": row.id, "gmail_message_id": row.gmail_message_id, "gmail_thread_id": row.gmail_thread_id,
             "sender": row.sender, "subject": row.subject, "classification": row.classification,
             "processing_status": row.processing_status, "sent_at": row.sent_at,
             "body_preview": row.body_text[:300], "attachment_count": counts.get(row.id, 0),
             "attachment_names": names.get(row.id, []), "created_at": row.created_at} for row in rows]


def start_background_monitor(settings: Settings, stop_event: threading.Event) -> threading.Thread:
    _SYNC_STATUS["automatic"] = True
    _SYNC_STATUS["next_sync_at"] = utcnow_iso()

    def monitor() -> None:
        while not stop_event.is_set():
            with get_session(settings) as session:
                connected = session.query(GmailAccountRow).filter(GmailAccountRow.status == "connected").count() > 0
            if connected and settings.project_oauth_token_path.is_file():
                try:
                    gmail_sync(settings)
                except Exception as exc:
                    _SYNC_STATUS["error"] = str(exc)[:500]
            next_sync = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=settings.gmail_sync_interval_seconds)
            _SYNC_STATUS["next_sync_at"] = next_sync.isoformat()
            stop_event.wait(settings.gmail_sync_interval_seconds)

    thread = threading.Thread(target=monitor, name="gmail-background-monitor", daemon=True)
    thread.start()
    return thread


def gmail_disconnect(settings: Settings, account_id: str) -> dict:
    token_path = _token_path(settings)
    now = utcnow_iso()
    with get_session(settings) as session:
        account = session.get(GmailAccountRow, account_id)
        if account is None:
            raise ValidationFailureError("Gmail account not found")
        account.status = "disconnected"
        account.updated_at = now
        session.commit()
    if token_path.is_file():
        token_path.unlink()
    return {"status": "disconnected", "account_id": account_id}

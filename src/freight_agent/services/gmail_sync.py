from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import re
from email.utils import parseaddr, parsedate_to_datetime
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


def _write_token_file(token_path: Path, payload: str) -> None:
    """Swap the token in one step so a reader running alongside never sees a half-written file."""
    temp = token_path.with_name(f"{token_path.name}.tmp")
    temp.write_text(payload, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, token_path)


def gmail_status(settings: Settings) -> dict:
    with get_session(settings) as session:
        accounts = session.query(GmailAccountRow).order_by(GmailAccountRow.updated_at.desc()).all()
    return {
        "configured": bool(settings.google_oauth_credentials_path),
        "accounts": [{"id": row.id, "email": row.email, "status": row.status, "last_synced_at": row.last_synced_at} for row in accounts],
        "stored_authorisation": _token_path(settings).is_file(),
        "send_enabled": True,
        "warning": "Email sending is only permitted through an explicit human-confirmed Draft Review action.",
        "queue": queue_status(settings),
        **_SYNC_STATUS,
        "automatic_status": "running" if _SYNC_STATUS["automatic"] else "stopped",
    }


def _refresh_failure_message(error: Exception) -> str:
    detail = f"{getattr(error, 'error', '')} {getattr(error, 'error_description', '')} {error}".lower()
    if "invalid_grant" in detail or "invalid_client" in detail or "revoked" in detail:
        return "Google has retired this authorisation, so the Gmail account must be connected again."
    return f"Google did not accept the stored Gmail authorisation ({type(error).__name__}). Try again in a moment."


def gmail_reconnect(settings: Settings) -> dict:
    """Re-attach the mailbox from the stored OAuth token; the operator must not re-consent to Google."""
    token_path = _token_path(settings)
    if not token_path.is_file():
        raise ConfigurationError("No stored Gmail authorisation to reconnect with; authorize the account first.")
    credentials = _read_credentials(settings)
    if credentials.expired and credentials.refresh_token:
        failure: Exception | None = None
        for attempt in range(2):
            try:
                credentials.refresh(Request())
                failure = None
                break
            except Exception as exc:
                failure = exc
                logger.warning("Gmail token refresh attempt %d failed: %s", attempt + 1, type(exc).__name__)
                time.sleep(1.0)
        if failure is not None:
            raise ValidationFailureError(_refresh_failure_message(failure)) from failure
        _write_token_file(token_path, credentials.to_json())

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    try:
        profile = service.users().getProfile(userId="me").execute()
    except Exception as exc:
        raise ValidationFailureError("Google refused the stored Gmail authorisation. Authorize the account again.") from exc
    email = str(profile.get("emailAddress") or "").strip()
    if not email:
        raise ValidationFailureError("Google returned no account address for the stored authorisation.")
    now = utcnow_iso()
    with get_session(settings) as session:
        account = session.query(GmailAccountRow).filter(GmailAccountRow.email == email).first()
        if account is None:
            account = GmailAccountRow(id=str(uuid4()), email=email, created_at=now)
            session.add(account)
        account.token_path = str(token_path)
        account.scopes_json = json.dumps(SCOPES)
        account.status = "connected"
        account.updated_at = now
        session.commit()
        account_id = account.id
    return {"account_id": account_id, "email": email, "status": "connected"}


def _message_sent_at(full: dict, headers: list) -> str | None:
    """The instant the mail was sent, not the instant it was imported."""
    raw = str(full.get("internalDate") or _header(headers, "date") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        try:
            return datetime.fromtimestamp(int(raw) / 1000, timezone.utc).replace(microsecond=0).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _sent_at_is_the_import_stamp(sent_at: str | None, created_at: str | None) -> bool:
    """True when the stored send time is really the moment the sync job copied the email."""
    if not sent_at or not created_at:
        return False
    try:
        return abs((datetime.fromisoformat(sent_at) - datetime.fromisoformat(created_at)).total_seconds()) <= 5
    except ValueError:
        return False


def repair_message_timestamps(settings: Settings, service, limit: int = 40) -> int:
    """Ask Google again for the send time of rows that stored the import time instead."""
    with get_session(settings) as session:
        rows = session.query(GmailMessageRow).filter(
            GmailMessageRow.classification.in_(QUOTATION_CLASSIFICATIONS)).all()
        candidates = [(row.id, row.gmail_message_id) for row in rows
                      if _sent_at_is_the_import_stamp(row.sent_at, row.created_at)][:limit]
    repaired = 0
    for row_id, gmail_message_id in candidates:
        try:
            fetched = service.users().messages().get(userId="me", id=gmail_message_id, format="metadata").execute()
        except Exception:  # noqa: BLE001 — a missing stamp is a display detail, not a failed sync
            logger.debug("timestamp repair skipped for %s", row_id)
            continue
        sent_at = _message_sent_at(fetched, fetched.get("payload", {}).get("headers", []))
        if not sent_at:
            continue
        with get_session(settings) as session:
            row = session.get(GmailMessageRow, row_id)
            if row is not None:
                row.sent_at = sent_at
                session.commit()
                repaired += 1
    return repaired


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
    _write_token_file(token_path, credential.to_json())
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
    """Run the inbound-quote path through to the draft; a person still approves and sends it."""
    from .supplier_workflow import run_quotation_pipeline

    action: dict | None = None
    outcome: dict | None = None
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
    _record_pipeline_notes(settings, (action or {}).get("id"), outcome)
    return status


def _record_pipeline_notes(settings: Settings, action_id: str | None, outcome: dict | None) -> None:
    """Leave the pipeline's own sentences on the supplier action so the email page can explain a short reading."""
    if action_id is None or not outcome:
        return
    notes = [str(note) for note in (outcome.get("notes") or []) if str(note).strip()]
    if not notes:
        return
    from ..db.models import SupplierQuotationActionRow

    with get_session(settings) as session:
        action = session.get(SupplierQuotationActionRow, action_id)
        if action is None:
            return
        already = action.notes or ""
        extra = [note for note in notes if note not in already]
        if extra:
            action.notes = " ".join([part for part in (already, *extra) if part]).strip()
            action.updated_at = utcnow_iso()
            session.commit()


def _reply_file(settings: Settings, digest: str, text: str) -> Path:
    """The reply body as a local text file, so the tested reply pipeline reads it unchanged."""
    directory = settings.workspace_dir.resolve() / "gmail_replies"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"reply-{digest[:12]}.txt"
    if not path.is_file():
        path.write_text(text, encoding="utf-8")
    return path


def _replied_quotation(session, message: GmailMessageRow) -> tuple[str, str, str] | None:
    """The (shipment, quotation, original document) this reply answers, or None when unclear.

    The Gmail thread comes first, then the address the quotation arrived from. A reply is only
    merged into one quotation; when several could match and none is named, a person links it.
    """
    from ..db.models import IntakeItemRow, QuotationRow

    def quotations(intake_ids: list[str]) -> dict[str, tuple[str, str, str]]:
        found: dict[str, tuple[str, str, str]] = {}
        for intake in session.query(IntakeItemRow).filter(IntakeItemRow.id.in_(intake_ids)).all():
            quote = session.query(QuotationRow).filter(QuotationRow.intake_item_id == intake.id).first()
            if quote is not None:
                found[quote.id] = (str(quote.shipment_id), str(quote.id), str(intake.id))
        return found

    if message.gmail_thread_id:
        thread_ids = [row.id for row in session.query(GmailMessageRow).filter(
            GmailMessageRow.gmail_thread_id == message.gmail_thread_id,
            GmailMessageRow.id != message.id).all()]
        intake_ids = [row.id for row in session.query(IntakeItemRow).filter(
            IntakeItemRow.gmail_message_id.in_(thread_ids)).all()] if thread_ids else []
        candidates = quotations(intake_ids)
        if len(candidates) == 1:
            return next(iter(candidates.values()))
    else:
        candidates = {}

    sender = _sender_address(message.sender)
    if sender:
        intake_ids = [row.id for row in session.query(IntakeItemRow).filter(
            IntakeItemRow.source_type.in_(["gmail_body", "gmail_attachment"]),
            IntakeItemRow.sender.ilike(f"%{sender}%")).all()]
        candidates = candidates or quotations(intake_ids)
        if len(candidates) > 1:
            text = f"{message.subject or ''}\n{message.body_text or ''}".lower()
            named = [item for item in candidates.values()
                     if (quote := session.get(QuotationRow, item[1])) and quote.quote_reference
                     and quote.quote_reference.lower() in text]
            if len(named) == 1:
                return named[0]
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _quotation_link(session, quotation_id: str) -> tuple[str, str, str] | None:
    """The quotation a person linked to this reply, with the document it was extracted from."""
    from ..db.models import QuotationRow

    quote = session.get(QuotationRow, quotation_id)
    if quote is None:
        raise ValidationFailureError("quotation not found")
    return (str(quote.shipment_id), str(quote.id), str(quote.intake_item_id))


def process_inbound_reply(settings: Settings, message_row_id: str,
                          quotation_id: str | None = None) -> str:
    """Merge a supplier's answer into the quotation it answers, then analyze and draft again.

    This is the loop the product asks for: answers update the quote, gaps are recomputed, and a
    further draft appears only while material points stay open. `quotation_id` is only for the
    rare reply that names no quotation, thread, or sender we can match.
    """
    from .replies import import_supplier_reply
    from .supplier_workflow import run_quotation_pipeline

    with get_session(settings) as session:
        message = session.get(GmailMessageRow, message_row_id)
        if message is None:
            raise ValidationFailureError("Gmail message not found")
        if message.classification != "SUPPLIER_QUOTATION_REPLY" and quotation_id is None:
            return str(message.processing_status)
        link = (_quotation_link(session, quotation_id) if quotation_id
                else _replied_quotation(session, message))
        if link is None:
            message.processing_status = "reply_needs_linking"
            session.commit()
            return "reply_needs_linking"
        shipment_id, target_quote_id, intake_id = link
        text = "\n".join(part for part in (message.subject, message.body_text) if part)
        digest = message.source_hash or hashlib.sha256(text.encode()).hexdigest()

    try:
        import_supplier_reply(settings, shipment_id, _reply_file(settings, digest, text),
                              quote_id=target_quote_id, on_duplicate="skip")
        outcome = run_quotation_pipeline(settings, intake_id)
    except Exception as exc:  # noqa: BLE001 — one unreadable reply must not stop the mailbox
        logger.warning("supplier reply %s could not be merged: %s", message_row_id,
                       type(exc).__name__, exc_info=True)
        status = "reply_failed"
    else:
        status = "draft_ready" if outcome.get("draft") else "reply_merged"
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


def _message_quotation_view(session, settings: Settings, quote) -> dict | None:
    """Everything the agent understood about this email's offer, in original currency and USD."""
    if quote is None:
        return None
    from . import money
    from .quotation_naming import name_for_quotation
    from ..db.models import CarrierRow

    carrier = session.get(CarrierRow, quote.carrier_id) if quote.carrier_id else None
    total = money.money_view(quote.total_known_cost, quote.currency, settings)
    base = money.money_view(quote.base_price, quote.currency, settings)
    return {
        "id": quote.id, "name": name_for_quotation(session, quote.id), "version": quote.version,
        "supplier": carrier.name if carrier else None, "quote_reference": quote.quote_reference,
        "origin": quote.origin, "destination": quote.destination,
        "transit_days_min": quote.transit_days_min, "transit_days_max": quote.transit_days_max,
        "valid_until": quote.valid_until, "delivery_scope": quote.delivery_scope,
        "insurance_included": None if quote.insurance_included is None else bool(quote.insurance_included),
        "customs_included": None if quote.customs_included is None else bool(quote.customs_included),
        "currency": quote.currency, "base_price": quote.base_price, "total_known_cost": quote.total_known_cost,
        "base_usd": base, "total_usd": total,
        "comparison_status": quote.comparison_status, "review_status": quote.review_status,
    }


def _queue_position(session, message) -> dict | None:
    """How many emails the reader will work through before this one, so the page can say so."""
    if message is None or message.processing_status not in QUEUE_STATUSES:
        return None
    waiting = tuple(QUEUE_STATUSES) + ("analyzing",)
    ahead = session.query(GmailMessageRow).filter(
        GmailMessageRow.classification.in_(QUOTATION_CLASSIFICATIONS),
        GmailMessageRow.processing_status.in_(waiting),
        ((GmailMessageRow.sent_at < message.sent_at) if message.sent_at else GmailMessageRow.sent_at.is_not(None)),
    ).count()
    total = session.query(GmailMessageRow).filter(
        GmailMessageRow.classification.in_(QUOTATION_CLASSIFICATIONS),
        GmailMessageRow.processing_status.in_(waiting)).count()
    return {"ahead": int(ahead), "in_queue": int(total)}


def gmail_message_detail(settings: Settings, message_row_id: str) -> dict:
    """Read the message as it stands; opening an email must never restart or destroy its analysis."""
    with get_session(settings) as session:
        message = session.get(GmailMessageRow, message_row_id)
        if message is None:
            raise ValidationFailureError("Gmail message not found")
        from ..db.models import IntakeItemRow, QuotationRow
        intake = session.query(IntakeItemRow).filter(IntakeItemRow.gmail_message_id == message_row_id).first()
        quote = session.query(QuotationRow).filter(QuotationRow.intake_item_id == intake.id).first() if intake else None
        attachments = session.query(GmailAttachmentRow).filter(GmailAttachmentRow.message_id == message_row_id).order_by(GmailAttachmentRow.created_at).all()
        from .quotation_naming import name_for_message
        payload = {
            "id": message.id,
            "name": name_for_message(session, message_row_id),
            "gmail_message_id": message.gmail_message_id,
            "gmail_thread_id": message.gmail_thread_id,
            "sender": message.sender,
            "subject": message.subject,
            "body_text": message.body_text,
            "classification": message.classification,
            "processing_status": message.processing_status,
            "sent_at": message.sent_at,
            "created_at": message.created_at,
            "intake_id": intake.id if intake else None,
            "quotation_id": quote.id if quote else None,
            "quotation": _message_quotation_view(session, settings, quote),
            "queue": _queue_position(session, message),
            "attachments": [{"id": attachment.id, "filename": attachment.filename, "mime_type": attachment.mime_type,
                             "size_bytes": attachment.size_bytes, "stored_path": attachment.stored_path,
                             "created_at": attachment.created_at} for attachment in attachments],
        }
    action = None
    if payload["intake_id"]:
        from .supplier_workflow import action_for_intake
        action = action_for_intake(settings, payload["intake_id"])
    draft = None
    if action:
        from .gmail_drafts import draft_for_action
        draft = draft_for_action(settings, action["id"])
    payload["analysis"] = action
    payload["draft"] = None if draft is None else {
        "id": draft["id"], "subject": draft["subject"], "body": draft["body"],
        "status": draft["status"], "recipient": draft["recipient"],
        "bound_recipient": draft["bound_recipient"], "recipient_locked": draft["recipient_locked"],
        "approved_recipient": draft.get("approved_recipient"), "sent_at": draft["sent_at"],
    }
    return payload


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
            sent_at = _message_sent_at(full, headers)

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
            if classification != "NON_QUOTATION":
                quotation_count += 1
            else:
                ignored_count += 1
                # Keep only synchronization identity and minimum state for unrelated mail; body was deliberately omitted above.

        try:
            repair_message_timestamps(settings, service)
        except Exception as exc:  # noqa: BLE001 — display stamps must never fail a sync
            logger.warning("timestamp repair skipped: %s", type(exc).__name__)

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
        from .quotation_naming import name_for_message
        labels = {row.id: name_for_message(session, row.id) for row in rows}
    counts: dict[str, int] = {}
    names: dict[str, list[str]] = {}
    for attachment in attachments:
        counts[attachment.message_id] = counts.get(attachment.message_id, 0) + 1
        names.setdefault(attachment.message_id, []).append(attachment.filename)
    return [{"id": row.id, "name": labels.get(row.id), "gmail_message_id": row.gmail_message_id,
             "gmail_thread_id": row.gmail_thread_id,
             "sender": row.sender, "subject": row.subject, "classification": row.classification,
             "processing_status": row.processing_status, "sent_at": row.sent_at,
             "body_preview": row.body_text[:300], "attachment_count": counts.get(row.id, 0),
             "attachment_names": names.get(row.id, []), "created_at": row.created_at} for row in rows]


QUEUE_STATUSES = ("received", "pending", "queued", "supplier_reply_pending")
QUOTATION_CLASSIFICATIONS = ("NEW_QUOTATION", "SUPPLIER_QUOTATION_REPLY", "UNCERTAIN")
QUEUE_IDLE_POLL_SECONDS = 4
_QUEUE_STATUS: dict[str, object] = {"running": False, "queued": 0, "reading_message_id": None,
                                    "last_read_message_id": None, "last_read_at": None, "error": None}


def _set_message_status(settings: Settings, message_row_id: str, status: str) -> None:
    with get_session(settings) as session:
        row = session.get(GmailMessageRow, message_row_id)
        if row is not None:
            row.processing_status = status
            session.commit()


def _claim_next_queued(settings: Settings) -> str | None:
    """Atomically take the email that arrived earliest and mark it as being read; newest must not jump the queue."""
    with get_session(settings) as session:
        row = session.query(GmailMessageRow).filter(
            GmailMessageRow.classification.in_(QUOTATION_CLASSIFICATIONS),
            GmailMessageRow.processing_status.in_(QUEUE_STATUSES),
        ).order_by(GmailMessageRow.sent_at.is_(None), GmailMessageRow.sent_at.asc(),
                   GmailMessageRow.created_at.asc(), GmailMessageRow.id.asc()).first()
        if row is None:
            return None
        message_row_id = row.id
        claimed = session.query(GmailMessageRow).filter(
            GmailMessageRow.id == message_row_id,
            GmailMessageRow.processing_status.in_(QUEUE_STATUSES),
        ).update({GmailMessageRow.processing_status: "analyzing"}, synchronize_session=False)
        session.commit()
    return message_row_id if claimed else None


def _read_queued_message(settings: Settings, message_row_id: str) -> str:
    with get_session(settings) as session:
        message = session.get(GmailMessageRow, message_row_id)
        classification = message.classification if message else None
    if classification == "SUPPLIER_QUOTATION_REPLY":
        return process_inbound_reply(settings, message_row_id)
    intake_id = ensure_message_quotation(settings, message_row_id)
    if intake_id is None:
        _set_message_status(settings, message_row_id, "not_a_quotation")
        return "not_a_quotation"
    return analyze_inbound_quotation(settings, message_row_id, intake_id)


def read_one_queued_message(settings: Settings) -> str | None:
    """Read exactly one queued email, so the local model digests the mailbox step by step."""
    message_row_id = _claim_next_queued(settings)
    if message_row_id is None:
        return None
    _QUEUE_STATUS["reading_message_id"] = message_row_id
    try:
        status = _read_queued_message(settings, message_row_id)
        _QUEUE_STATUS.update({"last_read_message_id": message_row_id, "last_read_at": utcnow_iso(), "error": None})
        logger.info("background reader completed %s: %s", message_row_id, status)
    except Exception as exc:  # noqa: BLE001 — one unreadable email must not stall the queue
        logger.warning("background reader failed for %s: %s", message_row_id, type(exc).__name__, exc_info=True)
        _set_message_status(settings, message_row_id, "extraction_failed")
        _QUEUE_STATUS["error"] = (f"{type(exc).__name__}: the reading stopped before it finished.")
    finally:
        _QUEUE_STATUS["reading_message_id"] = None
    return message_row_id


def queue_status(settings: Settings) -> dict:
    """How much still waits to be read and what the reader is busy with right now."""
    with get_session(settings) as session:
        queued = session.query(GmailMessageRow).filter(
            GmailMessageRow.classification.in_(QUOTATION_CLASSIFICATIONS),
            GmailMessageRow.processing_status.in_(QUEUE_STATUSES)).count()
        reading = session.query(GmailMessageRow).filter(
            GmailMessageRow.processing_status == "analyzing").count()
    return {**_QUEUE_STATUS, "queued": queued, "reading": reading}


def _requeue_interrupted(settings: Settings) -> None:
    """A run killed mid-read leaves its email stuck as 'analyzing'; put it back in the queue."""
    with get_session(settings) as session:
        session.query(GmailMessageRow).filter(
            GmailMessageRow.processing_status == "analyzing").update(
            {GmailMessageRow.processing_status: "received"}, synchronize_session=False)
        session.commit()


def start_background_reader(settings: Settings, stop_event: threading.Event) -> threading.Thread:
    _requeue_interrupted(settings)

    def reader() -> None:
        _QUEUE_STATUS["running"] = True
        while not stop_event.is_set():
            try:
                handled = read_one_queued_message(settings)
            except Exception as exc:  # noqa: BLE001 — the queue survives a database hiccup
                logger.warning("background reader cycle failed: %s", type(exc).__name__, exc_info=True)
                handled = None
            if handled is None:
                stop_event.wait(QUEUE_IDLE_POLL_SECONDS)
        _QUEUE_STATUS["running"] = False

    thread = threading.Thread(target=reader, name="gmail-background-reader", daemon=True)
    thread.start()
    return thread


def start_background_monitor(settings: Settings, stop_event: threading.Event) -> threading.Thread:
    _SYNC_STATUS["automatic"] = True
    _SYNC_STATUS["next_sync_at"] = utcnow_iso()
    start_background_reader(settings, stop_event)

    def monitor() -> None:
        while not stop_event.is_set():
            with get_session(settings) as session:
                connected = session.query(GmailAccountRow).filter(GmailAccountRow.status == "connected").count() > 0
            if not connected and settings.project_oauth_token_path.is_file():
                try:
                    gmail_reconnect(settings)
                    connected = True
                except Exception as exc:
                    logger.warning("automatic Gmail reconnect failed: %s", type(exc).__name__)
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

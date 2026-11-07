"""Human names for quotations: who quoted, and from where to where.

Filenames and email subjects are poor labels ("IMG_2874.pdf", "FW: quote"), so every list in
the app is titled from what the agent actually understood. This is display-only — the stored
filename and subject stay untouched for traceability.
"""
from __future__ import annotations

import re

# A route endpoint on a document is a full delivery address; people only need the city.
_ADDRESS_NOISE = re.compile(
    r"(warehouse|terminal|depot|zone|street|str\.?|avenue|ave\.?|road|rd\.?|building|bldg\.?"
    r"|block|floor|industrial|logistics|office|no\.?)", re.I)
_EMAIL_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw)\s*:\s*)+", re.I)
_SUPPLIER_NOISE = re.compile(r"\b(llc|ltd|limited|inc|gmbh|a\.s|as|ojsc|jsc|company|cargo|freight)\b", re.I)
_SYNTHETIC_NAME = re.compile(r"^gmail-[0-9a-z]+-(?:quotation|reply|body)\b", re.I)

MAX_NAME_LENGTH = 90


def place(value: str | None) -> str:
    """The shortest honest label for an endpoint: a city, not a warehouse address."""
    if not value:
        return ""
    text = str(value).strip()
    parts = [part.strip() for part in re.split(r"[,;]|\s{2,}", text) if part.strip()]
    for part in parts:
        if len(part) <= 30 and not _ADDRESS_NOISE.search(part):
            return part
    return (parts[0] if parts else text)[:30]


def supplier_label(value: str | None) -> str:
    """A supplier name people recognise, without the legal-form noise."""
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value)).strip().strip("-–—·,.;:")
    return cleaned[:60]


def email_subject_label(subject: str | None, fallback: str | None = None) -> str:
    text = _EMAIL_PREFIX.sub("", str(subject or "").strip())
    text = re.sub(r"\s+", " ", text)
    if not text:
        text = str(fallback or "").strip()
    return text[:MAX_NAME_LENGTH] if text else "Quotation"


def quotation_name(supplier: str | None = None, origin: str | None = None,
                   destination: str | None = None, fallback: str | None = None) -> str:
    """"Maersk Line — Shanghai to Los Angeles", degrading gracefully when a part is unknown."""
    who = supplier_label(supplier)
    from_place = place(origin)
    to_place = place(destination)
    route = f"{from_place} to {to_place}" if from_place and to_place else (from_place or to_place)
    if who and route:
        name = f"{who} — {route}"
    elif who or route:
        name = who or route
    else:
        name = ""
    if not name:
        name = email_subject_label(None, fallback) if fallback else "Quotation"
    elif len(name) < 12 and fallback:
        name = f"{name} — {email_subject_label(fallback)}"
    return name[:MAX_NAME_LENGTH].strip()


def sender_label(value: str | None) -> str:
    """The display name on someone's own mail, used until the supplier is confirmed by extraction."""
    from email.utils import parseaddr

    name, address = parseaddr(str(value or ""))
    cleaned = re.sub(r"\s+", " ", str(name or "")).strip()
    return cleaned or (address.split("@")[0] if address else "")


def _display_filename(value: str | None) -> str:
    """The workspace name the agent wrote for an email body ("gmail-38a54e88-quotation.txt") is a
    storage artifact, not something a person should read as a title."""
    text = str(value or "").strip()
    return "" if _SYNTHETIC_NAME.match(text) else text


def name_for_intake(session, intake_id: str) -> str:
    """What a list should call this document: who quoted, and from where to where."""
    from ..db.models import CarrierRow, IntakeItemRow, QuotationRow

    intake = session.get(IntakeItemRow, intake_id)
    if intake is None:
        return "Quotation"
    fallback = intake.subject or _display_filename(intake.original_filename)
    quote = session.query(QuotationRow).filter(QuotationRow.intake_item_id == intake.id).first()
    if quote is None:
        if fallback:
            return quotation_name(supplier=sender_label(intake.sender), fallback=fallback)
        # Nothing has been understood about this document yet, so the only honest title is who
        # sent it and when it arrived — otherwise several unanalysed emails share one label.
        return unread_label(intake.sender, intake.created_at)
    carrier = session.get(CarrierRow, quote.carrier_id) if quote.carrier_id else None
    return quotation_name(supplier=carrier.name if carrier else sender_label(intake.sender),
                          origin=quote.origin, destination=quote.destination, fallback=fallback)


def unread_label(sender: str | None, created_at: str | None) -> str:
    who = sender_label(sender)
    arrived = str(created_at or "")[:10]
    if who:
        return f"{who} — received {arrived}" if arrived else who
    return f"Quotation received {arrived}" if arrived else "Quotation"


def name_for_message(session, message_row_id: str) -> str:
    from ..db.models import GmailMessageRow, IntakeItemRow

    message = session.get(GmailMessageRow, message_row_id)
    if message is None:
        return "Quotation"
    intake = session.query(IntakeItemRow).filter(IntakeItemRow.gmail_message_id == message.id).first()
    if intake is not None:
        return name_for_intake(session, intake.id)
    return quotation_name(supplier=sender_label(message.sender),
                          fallback=message.subject or "Quotation email")


def name_for_quotation(session, quotation_id: str | None) -> str:
    """The same human label for a stored quote, used by drafts and comparison cards."""
    from ..db.models import QuotationRow

    if not quotation_id:
        return "Quotation"
    quote = session.get(QuotationRow, quotation_id)
    if quote is None:
        return "Quotation"
    if quote.intake_item_id:
        named = name_for_intake(session, str(quote.intake_item_id))
        if named != "Quotation":
            return named
    from ..db.models import CarrierRow

    carrier = session.get(CarrierRow, quote.carrier_id) if quote.carrier_id else None
    return quotation_name(supplier=carrier.name if carrier else quote.quote_reference,
                          origin=quote.origin, destination=quote.destination,
                          fallback=quote.quote_reference)


__all__ = ["email_subject_label", "place", "quotation_name", "sender_label", "supplier_label",
           "name_for_intake", "name_for_message", "name_for_quotation", "unread_label"]

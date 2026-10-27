from __future__ import annotations

import email
from email import policy
from pathlib import Path

from ..domain.exceptions import DocumentParsingError
from .base import ParseResult


def parse_eml(path: Path) -> ParseResult:
    """Standard-library EML parsing; attachments are read as text only, never executed."""
    try:
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    except Exception as exc:  # noqa: BLE001
        raise DocumentParsingError(f"cannot parse EML {path.name}: {exc}") from exc

    parts: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001
            continue
        if ctype == "text/html":
            content = _strip_html(content)
        parts.append(content)

    body = "\n\n".join(p for p in parts if p.strip())
    if not body:
        raise DocumentParsingError(f"EML {path.name} has no readable text body")
    return ParseResult(
        text=body,
        page_or_sheet_info="email body",
        subject=str(msg.get("subject") or "").strip() or None,
        sender=str(msg.get("from") or "").strip() or None,
        received_at=str(msg.get("date") or "").strip() or None,
        metadata={
            "message_id": str(msg.get("message-id") or ""),
            "quotation_id": str(msg.get("X-Freight-Quotation-ID") or ""),
            "shipment_id": str(msg.get("X-Freight-Shipment-ID") or ""),
        },
    )


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", "\n", text)
    import html as html_mod
    return html_mod.unescape(text)

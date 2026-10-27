from __future__ import annotations

from pathlib import Path

from ..domain.exceptions import DocumentParsingError
from .base import MAX_PAGES, ParseResult


def parse_pdf(path: Path) -> ParseResult:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise DocumentParsingError("pypdf is not installed") from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise DocumentParsingError(f"PDF {path.name} is encrypted; decryption is out of scope")
        pages = reader.pages[:MAX_PAGES]
        chunks = [f"[page {i + 1}]\n{p.extract_text() or ''}" for i, p in enumerate(pages)]
    except DocumentParsingError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentParsingError(f"cannot parse PDF {path.name}: {exc}") from exc
    return ParseResult(
        text="\n\n".join(chunks),
        page_or_sheet_info=f"{len(pages)} page(s)",
    )

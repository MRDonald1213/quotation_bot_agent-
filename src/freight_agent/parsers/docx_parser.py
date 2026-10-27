from __future__ import annotations

from pathlib import Path

from ..domain.exceptions import DocumentParsingError
from .base import ParseResult


def parse_docx(path: Path) -> ParseResult:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise DocumentParsingError("python-docx is not installed") from exc
    try:
        document = docx.Document(str(path))
        chunks = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                chunks.append(" | ".join(cells))
    except Exception as exc:  # noqa: BLE001
        raise DocumentParsingError(f"cannot parse DOCX {path.name}: {exc}") from exc
    if not chunks:
        raise DocumentParsingError(f"DOCX {path.name} has no readable text")
    return ParseResult(text="\n".join(chunks), page_or_sheet_info="docx")

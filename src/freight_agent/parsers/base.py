from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..domain.exceptions import DocumentParsingError

MAX_PAGES = 50
MAX_SHEETS = 20

SUPPORTED_EXTENSIONS = {".txt", ".eml", ".pdf", ".xlsx", ".docx", ".csv"}


@dataclass
class ParseResult:
    text: str
    page_or_sheet_info: str | None = None
    subject: str | None = None
    sender: str | None = None
    received_at: str | None = None
    metadata: dict = field(default_factory=dict)


def parse_file(path: Path) -> ParseResult:
    """Dispatch on extension. Content is untrusted data; macros/scripts/links are never executed."""
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise DocumentParsingError(
            f"unsupported file type {ext!r}; supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    if ext == ".txt":
        from .text_parser import parse_text
        return parse_text(path)
    if ext == ".csv":
        from .text_parser import parse_text
        result = parse_text(path)
        result.page_or_sheet_info = "csv"
        return result
    if ext == ".eml":
        from .eml_parser import parse_eml
        return parse_eml(path)
    if ext == ".pdf":
        from .pdf_parser import parse_pdf
        return parse_pdf(path)
    if ext == ".docx":
        from .docx_parser import parse_docx
        return parse_docx(path)
    if ext == ".xlsx":
        from .xlsx_parser import parse_xlsx
        return parse_xlsx(path)
    raise DocumentParsingError(f"unhandled extension {ext!r}")  # pragma: no cover

from __future__ import annotations

from pathlib import Path

from ..domain.exceptions import DocumentParsingError
from .base import ParseResult


def parse_text(path: Path) -> ParseResult:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DocumentParsingError(f"cannot read {path.name}: {exc}") from exc
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return ParseResult(text=raw.decode(encoding), page_or_sheet_info="page 1")
        except UnicodeDecodeError:
            continue
    raise DocumentParsingError(f"{path.name}: could not decode as text")

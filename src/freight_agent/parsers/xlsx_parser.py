from __future__ import annotations

from pathlib import Path

from ..domain.exceptions import DocumentParsingError
from .base import MAX_SHEETS, ParseResult


def parse_xlsx(path: Path) -> ParseResult:
    """Values only (data_only=True); formulas and macros are never executed."""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise DocumentParsingError("openpyxl is not installed") from exc
    try:
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        sheets = wb.sheetnames[:MAX_SHEETS]
        chunks: list[str] = []
        for name in sheets:
            ws = wb[name]
            chunks.append(f"[sheet {name}]")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    chunks.append(" | ".join(cells))
        wb.close()
    except DocumentParsingError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentParsingError(f"cannot parse XLSX {path.name}: {exc}") from exc
    return ParseResult(text="\n".join(chunks), page_or_sheet_info=f"{len(sheets)} sheet(s)")

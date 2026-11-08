"""Convert legacy office/Pages attachments to plain text with local LibreOffice."""
from __future__ import annotations
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from ..domain.exceptions import DocumentParsingError
from .base import ParseResult
from .pdf_parser import parse_pdf


def parse_office(path: Path) -> ParseResult:
    if path.suffix.lower() == ".pages" and zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                preview = next((name for name in archive.namelist() if name.lower().endswith("quicklook/preview.pdf")), None)
                if preview:
                    info = archive.getinfo(preview)
                    if info.file_size > 30 * 1024 * 1024:
                        raise DocumentParsingError("Pages PDF preview is too large")
                    with tempfile.TemporaryDirectory(prefix="freight-pages-") as folder:
                        pdf = Path(folder) / "preview.pdf"
                        pdf.write_bytes(archive.read(preview))
                        result = parse_pdf(pdf)
                        if result.text.strip(): return ParseResult(result.text, "Pages preview · " + str(result.page_or_sheet_info))
        except DocumentParsingError:
            raise
        except Exception as exc:
            raise DocumentParsingError("could not read Pages preview") from exc
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter:
        raise DocumentParsingError(f"{path.suffix.lower()} requires LibreOffice for local text conversion")
    with tempfile.TemporaryDirectory(prefix="freight-office-") as folder:
        output = Path(folder)
        profile = (output / "profile").as_uri()
        if path.suffix.lower() == ".xls":
            try:
                proc = subprocess.run([converter, "-env:UserInstallation=" + profile, "--headless", "--convert-to", "xlsx", "--outdir", str(output), str(path)],
                    capture_output=True, text=True, timeout=25, check=False)
            except subprocess.TimeoutExpired as exc:
                raise DocumentParsingError(f"conversion timed out for {path.name}") from exc
            converted = output / (path.stem + ".xlsx")
            if proc.returncode or not converted.is_file():
                raise DocumentParsingError(f"LibreOffice could not convert {path.name}")
            from .xlsx_parser import parse_xlsx
            return parse_xlsx(converted)
        try:
            proc = subprocess.run([converter, "-env:UserInstallation=" + profile, "--headless", "--convert-to", "txt:Text", "--outdir", str(output), str(path)],
                capture_output=True, text=True, timeout=25, check=False)
        except subprocess.TimeoutExpired as exc:
            raise DocumentParsingError(f"conversion timed out for {path.name}") from exc
        converted = output / (path.stem + ".txt")
        if proc.returncode or not converted.is_file():
            raise DocumentParsingError(f"LibreOffice could not convert {path.name}")
        text = converted.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            raise DocumentParsingError(f"{path.name} contains no readable text")
        return ParseResult(text, f"converted from {path.suffix.lower()}")

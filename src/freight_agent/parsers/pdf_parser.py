from __future__ import annotations

from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

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
        page_text = [p.extract_text() or "" for p in pages]
        chunks = [f"[page {i + 1}]\n{text}" for i, text in enumerate(page_text)]
    except DocumentParsingError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentParsingError(f"cannot parse PDF {path.name}: {exc}") from exc
    text = "\n\n".join(chunks).strip()
    if any(not item.strip() for item in page_text):
        scanned = [index for index, item in enumerate(page_text) if not item.strip()]
        ocr = _ocr_pdf(path, scanned)
        for block in ocr.split("\n\n"):
            match = __import__("re").match(r"\[page (\d+)\]\n(.*)", block, __import__("re").S)
            if match:
                index = int(match.group(1)) - 1
                if 0 <= index < len(page_text): page_text[index] = match.group(2).strip()
        text = "\n\n".join(f"[page {index + 1}]\n{page}" for index, page in enumerate(page_text) if page.strip()).strip()
    if not any(page.strip() for page in page_text):
        raise DocumentParsingError(f"PDF {path.name} has no readable text after local OCR")
    return ParseResult(text=text, page_or_sheet_info=f"{len(pages)} page(s) · text/OCR")


def _ocr_pdf(path: Path, pages: list[int]) -> str:
    """Use macOS Vision for local OCR when a PDF contains only scanned page images."""
    if platform.system() != "Darwin" or not shutil.which("swift"):
        raise DocumentParsingError(f"PDF {path.name} is image-only; local OCR is unavailable")
    lines = [
        "import Foundation", "import Vision", "import PDFKit", "import AppKit",
        "let url = URL(fileURLWithPath: CommandLine.arguments[1])",
        "guard let pdf = PDFDocument(url: url) else { fatalError(\"Unable to open PDF\") }",
        "let count = min(pdf.pageCount, 50)",
        "let wanted = Set(CommandLine.arguments[2].split(separator: Character(String(UnicodeScalar(44)!))).compactMap { Int($0) })",
        "for i in 0..<count {",
        "  if !wanted.contains(i) { continue }",
        "  guard let page = pdf.page(at: i) else { continue }",
        "  let image = page.thumbnail(of: NSSize(width: 1800, height: 2400), for: .mediaBox)",
        "  var rect = CGRect(origin: .zero, size: image.size)",
        "  guard let cg = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else { continue }",
        "  let request = VNRecognizeTextRequest()", "  request.recognitionLevel = .accurate",
        "  request.usesLanguageCorrection = true",
        "  try VNImageRequestHandler(cgImage: cg).perform([request])",
        "  let text = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: \"\\n\")",
        "  print(\"[page \\(i + 1)]\\n\\(text)\")", "}",
    ]
    with tempfile.TemporaryDirectory(prefix="freight-pdf-ocr-") as folder:
        source = Path(folder) / "read.swift"
        source.write_text("\n".join(lines), encoding="utf-8")
        try:
            result = subprocess.run(["swift", str(source), str(path), ",".join(str(index) for index in pages)], capture_output=True, text=True, timeout=90, check=False)
        except subprocess.TimeoutExpired as exc:
            raise DocumentParsingError(f"Local OCR timed out for {path.name}") from exc
    if result.returncode:
        raise DocumentParsingError(f"Local OCR failed for {path.name}")
    return result.stdout.strip()

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from ..config import Settings
from ..domain.enums import ClassificationStatus, ProcessingStatus, SourceType
from ..domain.exceptions import DuplicateIntakeError, ValidationFailureError
from ..domain.models import AuditEvent, IntakeItem
from ..parsers.base import SUPPORTED_EXTENSIONS, ParseResult, parse_file
from .common import with_session


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def register_intake(settings: Settings, shipment_id: str | None, path: Path,
                    source: str = "upload", sender: str | None = None,
                    subject: str | None = None, on_duplicate: str = "error") -> tuple[IntakeItem, bool]:
    """Validate, hash, copy into workspace, parse text locally, record provenance.

    Returns (intake_item, skipped_duplicate). on_duplicate: error|skip|reprocess.
    """
    if not path.is_file():
        raise ValidationFailureError(f"file not found: {path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValidationFailureError(
            f"unsupported file type {path.suffix!r}; supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    size = path.stat().st_size
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if size > max_bytes:
        raise ValidationFailureError(
            f"file is {size / 1_048_576:.1f} MB; MAX_FILE_SIZE_MB={settings.max_file_size_mb}")

    digest = sha256_file(path)
    with with_session(settings) as repos:
        existing = repos.find_intake_by_sha(digest)
        if existing is not None:
            if on_duplicate == "error":
                raise DuplicateIntakeError(digest, str(existing.id), existing.original_filename)
            if on_duplicate == "skip":
                return existing, True
            # reprocess: fall through, new intake record for same content

        stored_dir = settings.workspace_dir / "imported" / digest[:16]
        stored_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(ch for ch in path.name if ch.isprintable() and ch not in "/\\\x00")[:200]
        stored = stored_dir / safe_name
        if not stored.exists():
            shutil.copy2(path, stored)

        parsed: ParseResult = parse_file(path)
        item = IntakeItem(
            shipment_id=None,  # set below via UUID parse
            source_type=SourceType(source),
            original_filename=path.name,
            stored_path=str(stored),
            content_sha256=digest,
            sender=sender, subject=subject,
            received_at=None,
            raw_text=parsed.text,
            file_size_bytes=size,
            page_or_sheet_info=parsed.page_or_sheet_info,
            classification_status=ClassificationStatus.pending,
            processing_status=ProcessingStatus.pending,
        )
        if shipment_id:
            from uuid import UUID
            item.shipment_id = UUID(shipment_id)
        if parsed.sender and not sender:
            item.sender = parsed.sender[:300]
        if parsed.subject and not subject:
            item.subject = parsed.subject
        with repos.atomic():
            repos.add_intake(item)
            from ..db.models import SourceDocumentRow
            from uuid import uuid4
            repos.session.add(SourceDocumentRow(id=str(uuid4()), intake_item_id=str(item.id),
                filename=item.original_filename, source_kind="document", text=parsed.text,
                location=parsed.page_or_sheet_info))
            repos.add_audit(AuditEvent(entity_type="intake_item", entity_id=item.id, action="registered",
                                       details_json=json.dumps({"sha256": digest[:16], "file": path.name})))
        return item, False

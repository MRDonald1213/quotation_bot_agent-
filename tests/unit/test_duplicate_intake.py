from __future__ import annotations

import pytest

from freight_agent.domain.exceptions import DuplicateIntakeError
from freight_agent.services import intake as intake_service
from freight_agent.services.shipments import create_shipment


def _add(settings, src, **kw):
    return intake_service.register_intake(settings, None, src, **kw)


def test_duplicate_hash_errors_by_default(settings, quote_a_file):
    _add(settings, quote_a_file)
    with pytest.raises(DuplicateIntakeError) as exc:
        _add(settings, quote_a_file)
    assert "--on-duplicate" in str(exc.value)


def test_duplicate_skip_returns_existing(settings, quote_a_file):
    first, _ = _add(settings, quote_a_file)
    second, skipped = _add(settings, quote_a_file, on_duplicate="skip")
    assert skipped
    assert second.id == first.id


def test_duplicate_reprocess_creates_new_record(settings, quote_a_file):
    first, _ = _add(settings, quote_a_file)
    second, skipped = _add(settings, quote_a_file, on_duplicate="reprocess")
    assert not skipped
    assert second.id != first.id
    assert second.content_sha256 == first.content_sha256


def test_file_copied_to_workspace_with_provenance(settings, quote_a_file):
    item, _ = _add(settings, quote_a_file)
    from pathlib import Path
    stored = Path(item.stored_path)
    assert stored.exists()
    assert "imported" in str(stored)
    assert item.raw_text.startswith("Baltic Lines")


def test_size_limit_enforced(settings, quote_a_file, tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (settings.max_file_size_mb * 1024 * 1024 + 10))
    from freight_agent.domain.exceptions import ValidationFailureError
    with pytest.raises(ValidationFailureError):
        _add(settings, big)

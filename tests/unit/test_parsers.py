from __future__ import annotations

from pathlib import Path

import pytest

from freight_agent.domain.exceptions import DocumentParsingError
from freight_agent.parsers.base import parse_file
from freight_agent.services.intake import sha256_file


def test_parse_txt(tmp_path: Path):
    p = tmp_path / "q.txt"
    p.write_text("Base freight: 100 EUR\nvalid until 2026-12-01")
    r = parse_file(p)
    assert "100 EUR" in r.text


def test_parse_eml_metadata(tmp_path: Path):
    p = tmp_path / "q.eml"
    p.write_bytes(b"""From: sales@carrier.example\r
Subject: Quote QTR-1 for your RFQ\r
Date: Wed, 21 Oct 2026 09:14:00 +0400\r
Content-Type: text/plain; charset="utf-8"\r
\r
All-in rate 4650 EUR door to door.\r
""")
    r = parse_file(p)
    assert r.sender == "sales@carrier.example"
    assert r.subject == "Quote QTR-1 for your RFQ"
    assert "4650 EUR" in r.text


def test_unsupported_extension(tmp_path: Path):
    p = tmp_path / "evil.xlsm"
    p.write_bytes(b"macros are never executed")
    with pytest.raises(DocumentParsingError):
        parse_file(p)


def test_sha256_stable(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("same content")
    assert sha256_file(p) == sha256_file(p)

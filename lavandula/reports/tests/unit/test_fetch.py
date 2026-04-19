"""AC7 — content-type + magic-byte validation."""
from __future__ import annotations

import pytest


def test_ac7_magic_byte_rejects_non_pdf():
    from lavandula.reports.fetch_pdf import is_pdf_magic
    assert is_pdf_magic(b"%PDF-1.7\n..." + b"x" * 100)
    assert not is_pdf_magic(b"<!DOCTYPE html>" + b"x" * 100)
    assert not is_pdf_magic(b"")
    assert not is_pdf_magic(b"PDF-1.7")  # missing leading %
    assert not is_pdf_magic(b"%PDF")  # incomplete


def test_ac7_magic_byte_accepts_bom():
    """Some PDF writers precede with a UTF-8 BOM."""
    from lavandula.reports.fetch_pdf import is_pdf_magic
    assert is_pdf_magic(b"\xef\xbb\xbf%PDF-1.7\n")

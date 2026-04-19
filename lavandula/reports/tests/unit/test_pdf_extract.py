"""AC15 — active-content detection; AC18.2 — metadata sanitization."""
from __future__ import annotations

import pytest


def test_ac15_javascript_flag():
    from lavandula.reports.pdf_extract import scan_active_content
    pdf_bytes = b"""%PDF-1.4
1 0 obj <</JavaScript (app.alert('x'))>> endobj
%%EOF
"""
    flags = scan_active_content(pdf_bytes)
    assert flags["pdf_has_javascript"] == 1


def test_ac15_launch_flag():
    from lavandula.reports.pdf_extract import scan_active_content
    pdf_bytes = b"""%PDF-1.4
1 0 obj <</S /Launch /F (/usr/bin/sh)>> endobj
%%EOF
"""
    flags = scan_active_content(pdf_bytes)
    assert flags["pdf_has_launch"] == 1


def test_ac15_embedded_file_flag():
    from lavandula.reports.pdf_extract import scan_active_content
    pdf_bytes = b"""%PDF-1.4
1 0 obj <</Type /EmbeddedFile /Subtype /x>> endobj
%%EOF
"""
    flags = scan_active_content(pdf_bytes)
    assert flags["pdf_has_embedded"] == 1


def test_ac15_uri_action_flag():
    from lavandula.reports.pdf_extract import scan_active_content
    pdf_bytes = b"""%PDF-1.4
1 0 obj <</S /URI /URI (http://evil.example)>> endobj
%%EOF
"""
    flags = scan_active_content(pdf_bytes)
    assert flags["pdf_has_uri_actions"] == 1


def test_ac15_clean_pdf_has_no_flags():
    from lavandula.reports.pdf_extract import scan_active_content
    pdf_bytes = b"""%PDF-1.4
1 0 obj <</Type /Page>> endobj
%%EOF
"""
    flags = scan_active_content(pdf_bytes)
    assert flags["pdf_has_javascript"] == 0
    assert flags["pdf_has_launch"] == 0
    assert flags["pdf_has_embedded"] == 0
    assert flags["pdf_has_uri_actions"] == 0


def test_ac18_2_metadata_sanitized():
    from lavandula.reports.pdf_extract import sanitize_metadata_field
    dirty = "InDesign\x1b[31m\u200bDANGER\x00"
    clean = sanitize_metadata_field(dirty)
    for bad in ("\x1b", "\x00", "\u200b"):
        assert bad not in clean
    assert len(clean) <= 200


def test_ac18_2_metadata_truncated_to_200():
    from lavandula.reports.pdf_extract import sanitize_metadata_field
    dirty = "A" * 1000
    clean = sanitize_metadata_field(dirty)
    assert len(clean) <= 200


def test_ac18_2_none_passthrough():
    from lavandula.reports.pdf_extract import sanitize_metadata_field
    assert sanitize_metadata_field(None) is None

"""Tests for async_fetch_pdf.py (AC20, AC21)."""
from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from lavandula.reports.async_fetch_pdf import download, _head_or_skip
from lavandula.reports.http_client import FetchResult


def _make_client_mock():
    client = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_head_skip_non_pdf():
    client = _make_client_mock()
    client.head.return_value = FetchResult(
        status="ok", http_status=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        kind="pdf-head",
    )
    proceed, note = await _head_or_skip(client, "https://example.com/page")
    assert not proceed
    assert "text/html" in note


@pytest.mark.asyncio
async def test_head_proceed_on_pdf():
    client = _make_client_mock()
    client.head.return_value = FetchResult(
        status="ok", http_status=200,
        headers={"Content-Type": "application/pdf"},
        kind="pdf-head",
    )
    proceed, note = await _head_or_skip(client, "https://example.com/file.pdf")
    assert proceed


@pytest.mark.asyncio
async def test_head_fallback_on_405():
    client = _make_client_mock()
    client.head.return_value = FetchResult(
        status="server_error", http_status=405,
        kind="pdf-head",
    )
    proceed, note = await _head_or_skip(client, "https://example.com/file.pdf")
    assert proceed
    assert note == "head_not_supported"


@pytest.mark.asyncio
async def test_download_non_pdf_content_type():
    client = _make_client_mock()
    client.head.return_value = FetchResult(
        status="ok", http_status=200,
        headers={"Content-Type": "text/html"},
        kind="pdf-head",
    )
    result = await download("https://example.com/page", client)
    assert result.status == "blocked_content_type"


@pytest.mark.asyncio
async def test_download_magic_byte_mismatch():
    client = _make_client_mock()
    client.head.return_value = FetchResult(
        status="ok", http_status=200,
        headers={"Content-Type": "application/pdf"},
        kind="pdf-head",
    )
    client.get.return_value = FetchResult(
        status="ok", http_status=200, body=b"<html>not a pdf</html>",
        final_url="https://example.com/page",
        final_url_redacted="https://example.com/page",
        redirect_chain=["https://example.com/page"],
        redirect_chain_redacted=["https://example.com/page"],
        headers={"Content-Type": "application/pdf"},
        bytes_read=22, kind="pdf-get",
    )
    result = await download("https://example.com/page", client)
    assert result.status == "blocked_content_type"
    assert result.note == "magic_byte_mismatch"


@pytest.mark.asyncio
async def test_download_ok():
    pdf_body = b"%PDF-1.4 test content"
    sha = hashlib.sha256(pdf_body).hexdigest()

    client = _make_client_mock()
    client.head.return_value = FetchResult(
        status="ok", http_status=200,
        headers={"Content-Type": "application/pdf"},
        kind="pdf-head",
    )
    client.get.return_value = FetchResult(
        status="ok", http_status=200, body=pdf_body,
        final_url="https://example.com/report.pdf",
        final_url_redacted="https://example.com/report.pdf",
        redirect_chain=["https://example.com/report.pdf"],
        redirect_chain_redacted=["https://example.com/report.pdf"],
        headers={"Content-Type": "application/pdf"},
        bytes_read=len(pdf_body), kind="pdf-get",
    )

    with patch("lavandula.reports.async_fetch_pdf._validate_pdf_structure",
               return_value=(True, "")):
        result = await download("https://example.com/report.pdf", client)

    assert result.status == "ok"
    assert result.content_sha256 == sha
    assert result.body == pdf_body


@pytest.mark.asyncio
async def test_download_structure_validation_fail():
    pdf_body = b"%PDF-1.4 malformed"

    client = _make_client_mock()
    client.head.return_value = FetchResult(
        status="ok", http_status=200,
        headers={"Content-Type": "application/pdf"},
        kind="pdf-head",
    )
    client.get.return_value = FetchResult(
        status="ok", http_status=200, body=pdf_body,
        final_url="https://example.com/bad.pdf",
        final_url_redacted="https://example.com/bad.pdf",
        redirect_chain=["https://example.com/bad.pdf"],
        redirect_chain_redacted=["https://example.com/bad.pdf"],
        headers={"Content-Type": "application/pdf"},
        bytes_read=len(pdf_body), kind="pdf-get",
    )

    with patch("lavandula.reports.async_fetch_pdf._validate_pdf_structure",
               return_value=(False, "pdf_malformed:PdfReadError")):
        result = await download("https://example.com/bad.pdf", client)

    assert result.status == "server_error"
    assert "pdf_malformed" in result.note


@pytest.mark.asyncio
async def test_download_skip_validation():
    pdf_body = b"%PDF-1.4 content"
    sha = hashlib.sha256(pdf_body).hexdigest()

    client = _make_client_mock()
    client.head.return_value = FetchResult(
        status="ok", http_status=200,
        headers={"Content-Type": "application/pdf"},
        kind="pdf-head",
    )
    client.get.return_value = FetchResult(
        status="ok", http_status=200, body=pdf_body,
        final_url="https://example.com/report.pdf",
        final_url_redacted="https://example.com/report.pdf",
        redirect_chain=["https://example.com/report.pdf"],
        redirect_chain_redacted=["https://example.com/report.pdf"],
        headers={"Content-Type": "application/pdf"},
        bytes_read=len(pdf_body), kind="pdf-get",
    )

    result = await download(
        "https://example.com/report.pdf", client,
        validate_structure=False,
    )
    assert result.status == "ok"
    assert result.content_sha256 == sha


@pytest.mark.asyncio
async def test_download_get_failure():
    client = _make_client_mock()
    client.head.return_value = FetchResult(
        status="ok", http_status=200,
        headers={"Content-Type": "application/pdf"},
        kind="pdf-head",
    )
    client.get.return_value = FetchResult(
        status="network_error", http_status=None,
        final_url="https://example.com/fail.pdf",
        final_url_redacted="https://example.com/fail.pdf",
        redirect_chain=[], redirect_chain_redacted=[],
        kind="pdf-get", error="connection reset",
    )
    result = await download("https://example.com/fail.pdf", client)
    assert result.status == "network_error"

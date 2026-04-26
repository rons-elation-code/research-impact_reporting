"""Crawler v2 classification integration tests (Spec 0023, Fix #2).

Verifies that both async and sync crawlers:
- Call classify_first_page_v2 when a classifier_client is provided
- Pass material_type, material_group, event_type, reasoning to the DB upsert
- Fall back gracefully when classifier_client is None
- Fall back gracefully when classification errors occur
"""
from __future__ import annotations

import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from lavandula.reports.classify import ClassificationResult
from lavandula.reports.candidate_filter import Candidate


# --------------- helpers ---------------

def _minimal_pdf() -> bytes:
    """Single-page PDF that pypdf can parse with extractable text."""
    from pypdf import PdfWriter
    w = PdfWriter()
    from pypdf._page import PageObject
    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        TextStringObject,
        DecodedStreamObject,
    )
    page = PageObject.create_blank_page(width=72, height=72)
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 50 Td (Annual Report 2025) Tj ET")
    page[NameObject("/Contents")] = stream
    font_dict = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): font_dict,
        })
    })
    page[NameObject("/Resources")] = resources
    w.add_page(page)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _make_candidate(**overrides) -> Candidate:
    defaults = dict(
        url="https://example.org/report.pdf",
        referring_page_url="https://example.org/",
        discovered_via="homepage-link",
        hosting_platform="own-domain",
        attribution_confidence="own_domain",
        anchor_text="Annual Report 2025",
    )
    defaults.update(overrides)
    return Candidate(**defaults)


def _fake_cls_result(**overrides) -> ClassificationResult:
    defaults = dict(
        classification="annual",
        classification_confidence=0.95,
        reasoning="Clearly an annual report",
        classifier_model="claude-haiku-4-5",
        input_tokens=500,
        output_tokens=50,
        material_type="annual_report",
        material_group="reports",
        event_type=None,
    )
    defaults.update(overrides)
    return ClassificationResult(**defaults)


# ======================= ASYNC CRAWLER TESTS =======================


@dataclass
class _FakeDownloadOutcome:
    body: bytes = b""
    status: str = "ok"
    content_sha256: str = "abc123"
    final_url: str = "https://example.org/report.pdf"
    final_url_redacted: str = "https://example.org/report.pdf"
    redirect_chain_redacted: list[str] | None = None
    note: str = ""


@pytest.mark.asyncio
async def test_async_process_download_passes_v2_fields():
    """_process_download passes v2 classifier fields to UpsertReportRequest."""
    from lavandula.reports.async_crawler import _process_download
    from lavandula.reports.async_db_writer import UpsertReportRequest

    enqueued: list[object] = []
    loop = asyncio.get_running_loop()

    async def mock_enqueue(req):
        enqueued.append(req)
        fut = loop.create_future()
        fut.set_result(True)
        return fut

    db_actor = MagicMock()
    db_actor.enqueue = mock_enqueue

    pdf_bytes = _minimal_pdf()
    outcome = _FakeDownloadOutcome(body=pdf_bytes)

    cls_result = _fake_cls_result()

    mock_archive = MagicMock()
    mock_archive.put = MagicMock()

    stats = MagicMock()
    stats.pdfs_downloaded = 0
    stats.bytes_downloaded = 0

    pdf_pool = ThreadPoolExecutor(max_workers=1)

    mock_classifier = MagicMock()

    with patch("lavandula.reports.async_crawler.async_download", return_value=outcome), \
         patch("lavandula.reports.async_crawler._ensure_taxonomy"), \
         patch("lavandula.reports.async_crawler.get_taxonomy") as mock_tax, \
         patch("lavandula.reports.async_crawler.classify_first_page_v2", return_value=cls_result):

        await _process_download(
            ein="12-3456789",
            cand=_make_candidate(),
            client=MagicMock(),
            db_actor=db_actor,
            archive=mock_archive,
            run_id="test-run",
            seed_etld1="example.org",
            stats=stats,
            pdf_thread_pool=pdf_pool,
            classifier_client=mock_classifier,
        )

    pdf_pool.shutdown(wait=True)

    upsert_reqs = [r for r in enqueued if isinstance(r, UpsertReportRequest)]
    assert len(upsert_reqs) == 1

    req = upsert_reqs[0]
    assert req.material_type == "annual_report"
    assert req.material_group == "reports"
    assert req.event_type is None
    assert req.reasoning == "Clearly an annual report"
    assert req.classification == "annual"
    assert req.classification_confidence == 0.95
    assert req.classifier_version == 2


@pytest.mark.asyncio
async def test_async_process_download_no_classifier_client():
    """Without classifier_client, v2 fields are None."""
    from lavandula.reports.async_crawler import _process_download
    from lavandula.reports.async_db_writer import UpsertReportRequest

    enqueued: list[object] = []
    loop = asyncio.get_running_loop()

    async def mock_enqueue(req):
        enqueued.append(req)
        fut = loop.create_future()
        fut.set_result(True)
        return fut

    db_actor = MagicMock()
    db_actor.enqueue = mock_enqueue

    pdf_bytes = _minimal_pdf()
    outcome = _FakeDownloadOutcome(body=pdf_bytes)

    mock_archive = MagicMock()
    mock_archive.put = MagicMock()

    stats = MagicMock()
    stats.pdfs_downloaded = 0
    stats.bytes_downloaded = 0

    pdf_pool = ThreadPoolExecutor(max_workers=1)

    with patch("lavandula.reports.async_crawler.async_download", return_value=outcome), \
         patch("lavandula.reports.async_crawler._ensure_taxonomy"):

        await _process_download(
            ein="12-3456789",
            cand=_make_candidate(),
            client=MagicMock(),
            db_actor=db_actor,
            archive=mock_archive,
            run_id="test-run",
            seed_etld1="example.org",
            stats=stats,
            pdf_thread_pool=pdf_pool,
            classifier_client=None,
        )

    pdf_pool.shutdown(wait=True)

    upsert_reqs = [r for r in enqueued if isinstance(r, UpsertReportRequest)]
    assert len(upsert_reqs) == 1

    req = upsert_reqs[0]
    assert req.material_type is None
    assert req.material_group is None
    assert req.event_type is None
    assert req.reasoning is None
    assert req.classification is None


@pytest.mark.asyncio
async def test_async_process_download_classifier_error_fallback():
    """When classify_first_page_v2 raises, v2 fields are None (graceful fallback)."""
    from lavandula.reports.async_crawler import _process_download
    from lavandula.reports.async_db_writer import UpsertReportRequest

    enqueued: list[object] = []
    loop = asyncio.get_running_loop()

    async def mock_enqueue(req):
        enqueued.append(req)
        fut = loop.create_future()
        fut.set_result(True)
        return fut

    db_actor = MagicMock()
    db_actor.enqueue = mock_enqueue

    pdf_bytes = _minimal_pdf()
    outcome = _FakeDownloadOutcome(body=pdf_bytes)

    mock_archive = MagicMock()
    mock_archive.put = MagicMock()

    stats = MagicMock()
    stats.pdfs_downloaded = 0
    stats.bytes_downloaded = 0

    pdf_pool = ThreadPoolExecutor(max_workers=1)

    def _raise(*a, **kw):
        raise RuntimeError("API down")

    with patch("lavandula.reports.async_crawler.async_download", return_value=outcome), \
         patch("lavandula.reports.async_crawler._ensure_taxonomy"), \
         patch("lavandula.reports.async_crawler.get_taxonomy"), \
         patch("lavandula.reports.async_crawler.classify_first_page_v2", side_effect=_raise):

        await _process_download(
            ein="12-3456789",
            cand=_make_candidate(),
            client=MagicMock(),
            db_actor=db_actor,
            archive=mock_archive,
            run_id="test-run",
            seed_etld1="example.org",
            stats=stats,
            pdf_thread_pool=pdf_pool,
            classifier_client=MagicMock(),
        )

    pdf_pool.shutdown(wait=True)

    upsert_reqs = [r for r in enqueued if isinstance(r, UpsertReportRequest)]
    assert len(upsert_reqs) == 1

    req = upsert_reqs[0]
    assert req.material_type is None
    assert req.material_group is None
    assert req.event_type is None
    assert req.reasoning is None


# ======================= SYNC CRAWLER TESTS =======================


@pytest.mark.parametrize("with_classifier", [True, False])
def test_sync_process_org_v2_fields(with_classifier):
    """Sync process_org passes v2 fields when classifier_client is provided."""
    from lavandula.reports.crawler import process_org

    upsert_calls: list[dict] = []
    original_upsert = None

    def capture_upsert(engine, **kwargs):
        upsert_calls.append(kwargs)

    cls_result = _fake_cls_result()

    mock_engine = MagicMock()
    mock_archive = MagicMock()
    mock_client = MagicMock()

    cand = _make_candidate()
    pdf_bytes = _minimal_pdf()

    download_outcome = MagicMock()
    download_outcome.status = "ok"
    download_outcome.body = pdf_bytes
    download_outcome.content_sha256 = "abc123"
    download_outcome.final_url = "https://example.org/report.pdf"
    download_outcome.final_url_redacted = "https://example.org/report.pdf"
    download_outcome.redirect_chain_redacted = None
    download_outcome.note = ""

    mock_classifier_client = MagicMock() if with_classifier else None

    with patch("lavandula.reports.crawler.per_org_candidates", return_value=[cand]), \
         patch("lavandula.reports.crawler.fetch_pdf.download", return_value=download_outcome), \
         patch("lavandula.reports.crawler.db_writer.upsert_report", side_effect=capture_upsert), \
         patch("lavandula.reports.crawler.db_writer.record_fetch"), \
         patch("lavandula.reports.crawler.db_writer.upsert_crawled_org"), \
         patch("lavandula.reports.crawler._ensure_taxonomy"), \
         patch("lavandula.reports.crawler.get_taxonomy"), \
         patch("lavandula.reports.crawler.classify_first_page_v2", return_value=cls_result):

        process_org(
            ein="12-3456789",
            website="https://example.org",
            engine=mock_engine,
            archive=mock_archive,
            run_id="test-run",
            client=mock_client,
            classifier_client=mock_classifier_client,
        )

    assert len(upsert_calls) == 1
    call = upsert_calls[0]

    if with_classifier:
        assert call["material_type"] == "annual_report"
        assert call["material_group"] == "reports"
        assert call["event_type"] is None
        assert call["reasoning"] == "Clearly an annual report"
        assert call["classification"] == "annual"
        assert call["classifier_version"] == 2
    else:
        assert call["material_type"] is None
        assert call["material_group"] is None
        assert call["event_type"] is None
        assert call["reasoning"] is None
        assert call["classification"] is None


def test_sync_process_org_event_type_passthrough():
    """Sync process_org passes event_type when classifier returns one."""
    from lavandula.reports.crawler import process_org

    upsert_calls: list[dict] = []

    def capture_upsert(engine, **kwargs):
        upsert_calls.append(kwargs)

    cls_result = _fake_cls_result(
        classification="other",
        material_type="event_invitation",
        material_group="invitations",
        event_type="gala",
        reasoning="Gala invitation card",
    )

    mock_engine = MagicMock()
    mock_archive = MagicMock()
    mock_client = MagicMock()

    cand = _make_candidate()
    pdf_bytes = _minimal_pdf()

    download_outcome = MagicMock()
    download_outcome.status = "ok"
    download_outcome.body = pdf_bytes
    download_outcome.content_sha256 = "def456"
    download_outcome.final_url = "https://example.org/gala.pdf"
    download_outcome.final_url_redacted = "https://example.org/gala.pdf"
    download_outcome.redirect_chain_redacted = None
    download_outcome.note = ""

    with patch("lavandula.reports.crawler.per_org_candidates", return_value=[cand]), \
         patch("lavandula.reports.crawler.fetch_pdf.download", return_value=download_outcome), \
         patch("lavandula.reports.crawler.db_writer.upsert_report", side_effect=capture_upsert), \
         patch("lavandula.reports.crawler.db_writer.record_fetch"), \
         patch("lavandula.reports.crawler.db_writer.upsert_crawled_org"), \
         patch("lavandula.reports.crawler._ensure_taxonomy"), \
         patch("lavandula.reports.crawler.get_taxonomy"), \
         patch("lavandula.reports.crawler.classify_first_page_v2", return_value=cls_result):

        process_org(
            ein="12-3456789",
            website="https://example.org",
            engine=mock_engine,
            archive=mock_archive,
            run_id="test-run",
            client=mock_client,
            classifier_client=MagicMock(),
        )

    assert len(upsert_calls) == 1
    call = upsert_calls[0]
    assert call["material_type"] == "event_invitation"
    assert call["material_group"] == "invitations"
    assert call["event_type"] == "gala"
    assert call["reasoning"] == "Gala invitation card"

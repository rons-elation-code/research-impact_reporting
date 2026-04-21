from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace


def _stub_fetch_result(*, status="ok", body=b"", final_url="https://example.org/robots.txt"):
    return SimpleNamespace(
        status=status,
        body=body,
        final_url=final_url,
        final_url_redacted=final_url,
        redirect_chain=[final_url],
        redirect_chain_redacted=[final_url],
        http_status=200 if status == "ok" else None,
        elapsed_ms=1,
        note="",
        error="",
        headers={},
        bytes_read=len(body),
    )


def test_process_org_logs_extract_failure(tmp_path, monkeypatch):
    from lavandula.reports import crawler, schema, fetch_pdf
    from lavandula.reports.candidate_filter import Candidate

    conn = schema.ensure_db(tmp_path / "reports.db")
    archive_dir = tmp_path / "raw"
    archive_dir.mkdir()

    monkeypatch.setattr(
        crawler,
        "per_org_candidates",
        lambda **kwargs: [
            Candidate(
                url="https://example.org/report.pdf",
                anchor_text="Annual Report",
                referring_page_url="https://example.org/reports",
                discovered_via="subpage-link",
                hosting_platform="own-domain",
                attribution_confidence="own_domain",
            )
        ],
    )
    monkeypatch.setattr(
        fetch_pdf,
        "download",
        lambda *args, **kwargs: fetch_pdf.DownloadOutcome(
            status="ok",
            url="https://example.org/report.pdf",
            final_url="https://example.org/report.pdf",
            final_url_redacted="https://example.org/report.pdf",
            redirect_chain=[],
            redirect_chain_redacted=[],
            content_sha256="a" * 64,
            bytes_read=128,
            content_type="application/pdf",
            body=b"%PDF-1.7\nfake",
        ),
    )
    monkeypatch.setattr(crawler._archive, "write_pdf", lambda *args, **kwargs: archive_dir / "a.pdf")
    monkeypatch.setattr(
        crawler,
        "scan_active_content",
        lambda body: {
            "pdf_has_javascript": 0,
            "pdf_has_launch": 0,
            "pdf_has_embedded": 0,
            "pdf_has_uri_actions": 0,
        },
    )

    fake_pypdf = ModuleType("pypdf")

    class BrokenPdfReader:
        def __init__(self, _data):
            raise RuntimeError("bad\npdf\tdata")

    fake_pypdf.PdfReader = BrokenPdfReader
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    class StubClient:
        def get(self, url, kind, seed_etld1=None):
            return _stub_fetch_result(status="not_found", final_url=url)

    result = crawler.process_org(
        ein="000000001",
        website="https://example.org",
        client=StubClient(),
        conn=conn,
        archive_dir=archive_dir,
    )

    assert result.fetched_count == 1
    row = conn.execute(
        "SELECT kind, fetch_status, notes FROM fetch_log WHERE kind='extract'"
    ).fetchone()
    assert row is not None
    assert row[0] == "extract"
    assert row[1] == "server_error"
    assert "\n" not in (row[2] or "")

    conn.close()


def test_process_org_logs_extract_success(tmp_path, monkeypatch):
    from lavandula.reports import crawler, schema, fetch_pdf
    from lavandula.reports.candidate_filter import Candidate

    conn = schema.ensure_db(tmp_path / "reports.db")
    archive_dir = tmp_path / "raw"
    archive_dir.mkdir()

    monkeypatch.setattr(
        crawler,
        "per_org_candidates",
        lambda **kwargs: [
            Candidate(
                url="https://example.org/2024-impact-report.pdf",
                anchor_text="Annual Report",
                referring_page_url="https://example.org/reports",
                discovered_via="subpage-link",
                hosting_platform="own-domain",
                attribution_confidence="own_domain",
            )
        ],
    )
    monkeypatch.setattr(
        fetch_pdf,
        "download",
        lambda *args, **kwargs: fetch_pdf.DownloadOutcome(
            status="ok",
            url="https://example.org/2024-impact-report.pdf",
            final_url="https://example.org/2024-impact-report.pdf",
            final_url_redacted="https://example.org/2024-impact-report.pdf",
            redirect_chain=[],
            redirect_chain_redacted=[],
            content_sha256="b" * 64,
            bytes_read=128,
            content_type="application/pdf",
            body=b"%PDF-1.7\nfake",
        ),
    )
    monkeypatch.setattr(crawler._archive, "write_pdf", lambda *args, **kwargs: archive_dir / "b.pdf")
    monkeypatch.setattr(
        crawler,
        "scan_active_content",
        lambda body: {
            "pdf_has_javascript": 0,
            "pdf_has_launch": 0,
            "pdf_has_embedded": 0,
            "pdf_has_uri_actions": 0,
        },
    )

    fake_pypdf = ModuleType("pypdf")

    class GoodPage:
        def extract_text(self):
            return "Annual Report"

    class GoodPdfReader:
        def __init__(self, _data):
            self.pages = [GoodPage()]
            self.metadata = {}

    fake_pypdf.PdfReader = GoodPdfReader
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    class StubClient:
        def get(self, url, kind, seed_etld1=None):
            return _stub_fetch_result(status="not_found", final_url=url)

    result = crawler.process_org(
        ein="000000002",
        website="https://example.org",
        client=StubClient(),
        conn=conn,
        archive_dir=archive_dir,
    )

    assert result.fetched_count == 1
    row = conn.execute(
        "SELECT kind, fetch_status, notes FROM fetch_log WHERE kind='extract'"
    ).fetchone()
    assert row is not None
    assert row[0] == "extract"
    assert row[1] == "ok"
    assert "page_count=1" in (row[2] or "")
    stored = conn.execute(
        "SELECT report_year, report_year_source FROM reports WHERE content_sha256 = ?",
        ("b" * 64,),
    ).fetchone()
    assert stored["report_year"] == 2024
    assert stored["report_year_source"] == "filename"

    conn.close()


def test_process_org_counts_only_report_classifications(tmp_path, monkeypatch):
    from lavandula.reports import crawler, schema, fetch_pdf
    from lavandula.reports.candidate_filter import Candidate

    conn = schema.ensure_db(tmp_path / "reports.db")
    archive_dir = tmp_path / "raw"
    archive_dir.mkdir()

    monkeypatch.setattr(
        crawler,
        "per_org_candidates",
        lambda **kwargs: [
            Candidate(
                url="https://example.org/report.pdf",
                anchor_text="IRS 990",
                referring_page_url="https://example.org/reports",
                discovered_via="subpage-link",
                hosting_platform="own-domain",
                attribution_confidence="own_domain",
            )
        ],
    )
    monkeypatch.setattr(
        fetch_pdf,
        "download",
        lambda *args, **kwargs: fetch_pdf.DownloadOutcome(
            status="ok",
            url="https://example.org/report.pdf",
            final_url="https://example.org/report.pdf",
            final_url_redacted="https://example.org/report.pdf",
            redirect_chain=[],
            redirect_chain_redacted=[],
            content_sha256="c" * 64,
            bytes_read=128,
            content_type="application/pdf",
            body=b"%PDF-1.7\nfake",
        ),
    )
    monkeypatch.setattr(crawler._archive, "write_pdf", lambda *args, **kwargs: archive_dir / "c.pdf")
    monkeypatch.setattr(
        crawler,
        "scan_active_content",
        lambda body: {
            "pdf_has_javascript": 0,
            "pdf_has_launch": 0,
            "pdf_has_embedded": 0,
            "pdf_has_uri_actions": 0,
        },
    )

    fake_pypdf = ModuleType("pypdf")

    class GoodPage:
        def extract_text(self):
            return "Form 990"

    class GoodPdfReader:
        def __init__(self, _data):
            self.pages = [GoodPage()]
            self.metadata = {}

    fake_pypdf.PdfReader = GoodPdfReader
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    # TICK-002: classification is no longer invoked from process_org —
    # confirmed_report_count is therefore always 0. The post-crawl
    # `classify_null.py` pass fills classification later.
    class StubClient:
        def get(self, url, kind, seed_etld1=None):
            return _stub_fetch_result(status="not_found", final_url=url)

    result = crawler.process_org(
        ein="000000003",
        website="https://example.org",
        client=StubClient(),
        conn=conn,
        archive_dir=archive_dir,
    )

    assert result.fetched_count == 1
    assert result.confirmed_report_count == 0

    # Stored row has classification=NULL (deferred to classify_null.py).
    row = conn.execute(
        "SELECT classification FROM reports WHERE content_sha256 = ?",
        ("c" * 64,),
    ).fetchone()
    assert row is not None
    assert row["classification"] is None

    conn.close()

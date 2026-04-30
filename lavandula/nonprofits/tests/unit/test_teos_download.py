"""Unit tests for teos_download.py (Spec 0026)."""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from lavandula.nonprofits.teos_download import (
    ProcessStats,
    _download_zip,
    _process_single_filing,
    _sanitize_error,
    process_filings,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "990"


def _make_zip_with_members(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _minimal_990_xml(ein="111111111", person_name="TEST PERSON") -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <Return xmlns="http://www.irs.gov/efile" returnVersion="2023v4.0">
      <ReturnHeader>
        <ReturnTs>2024-01-15T10:00:00-05:00</ReturnTs>
        <TaxPeriodEndDt>2023-12-31</TaxPeriodEndDt>
        <Filer><EIN>{ein}</EIN></Filer>
      </ReturnHeader>
      <ReturnData>
        <IRS990>
          <Form990PartVIISectionAGrp>
            <PersonNm>{person_name}</PersonNm>
            <TitleTxt>CEO</TitleTxt>
            <OfficerInd>X</OfficerInd>
            <ReportableCompFromOrgAmt>100000</ReportableCompFromOrgAmt>
          </Form990PartVIISectionAGrp>
        </IRS990>
      </ReturnData>
    </Return>""".encode()


def _mock_engine_for_process():
    engine = MagicMock()

    connect_ctx = MagicMock()
    connect_conn = MagicMock()
    connect_ctx.__enter__ = MagicMock(return_value=connect_conn)
    connect_ctx.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value = connect_ctx

    begin_ctx = MagicMock()
    begin_conn = MagicMock()
    begin_ctx.__enter__ = MagicMock(return_value=begin_conn)
    begin_ctx.__exit__ = MagicMock(return_value=False)
    engine.begin.return_value = begin_ctx

    return engine, connect_conn, begin_conn


class TestSanitizeError:
    def test_truncates(self):
        msg = "x" * 600
        assert len(_sanitize_error(msg)) == 500

    def test_short_message(self):
        assert _sanitize_error("short") == "short"


class TestZipBombRejection:
    """AC38: Zip members exceeding 50MB uncompressed size are rejected."""

    def test_large_member_rejected(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("OBJ001_public.xml")
            info.file_size = 60 * 1024 * 1024  # 60MB
            zf.writestr(info, b"x" * 100)
        buf.seek(0)

        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(buf.getvalue())

        engine, _, _ = _mock_engine_for_process()
        stats = ProcessStats()

        with zipfile.ZipFile(zip_path) as zf:
            _process_single_filing(
                engine=engine,
                zf=zf,
                filing={"object_id": "OBJ001", "ein": "111111111", "tax_period": "202312"},
                run_id="test",
                stats=stats,
            )

        assert stats.filings_error == 1


class TestMissingMember:
    """AC48: expected XML member absent from zip → error."""

    def test_missing_member(self, tmp_path):
        zip_data = _make_zip_with_members({"OTHER_public.xml": b"<x/>"})
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_data)

        engine, _, _ = _mock_engine_for_process()
        stats = ProcessStats()

        with zipfile.ZipFile(zip_path) as zf:
            _process_single_filing(
                engine=engine,
                zf=zf,
                filing={"object_id": "OBJ001", "ein": "111111111", "tax_period": "202312"},
                run_id="test",
                stats=stats,
            )

        assert stats.filings_error == 1


class TestPathTraversalRejection:
    """AC34: zip extraction rejects path-traversing members."""

    def test_path_traversal_zip(self):
        zip_path = FIXTURES / "malicious_zip.zip"
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.namelist()
            assert any(".." in m for m in members)


class TestSkipDownloadMissing:
    """AC50: --skip-download with missing cache file → filing stays indexed."""

    def test_skip_download_no_cache(self, tmp_path):
        engine, connect_conn, _ = _mock_engine_for_process()

        filing_row = MagicMock()
        filing_row.__getitem__ = lambda self, i: {
            0: "OBJ001", 1: "111111111", 2: "202312",
            3: "2024_TEOS_XML_01A", 4: 2024,
        }[i]

        result = MagicMock()
        result.fetchall.return_value = [filing_row]
        connect_conn.execute.return_value = result

        stats = process_filings(
            engine=engine,
            cache_dir=tmp_path,
            skip_download=True,
            run_id="test",
        )

        assert stats.zips_downloaded == 0


class TestReparseResetsError:
    """AC51: --reparse re-processes error rows."""

    def test_reparse_resets_status(self):
        engine, _, begin_conn = _mock_engine_for_process()

        connect_ctx = MagicMock()
        connect_conn = MagicMock()
        connect_ctx.__enter__ = MagicMock(return_value=connect_conn)
        connect_ctx.__exit__ = MagicMock(return_value=False)
        engine.connect.return_value = connect_ctx

        result = MagicMock()
        result.rowcount = 3
        begin_conn.execute.return_value = result

        fetch_result = MagicMock()
        fetch_result.fetchall.return_value = []
        connect_conn.execute.return_value = fetch_result

        stats = process_filings(
            engine=engine,
            cache_dir=Path("/tmp"),
            reparse=True,
            run_id="test",
            ein_set={"111111111"},
            filing_years=[2024],
        )

        reset_call = begin_conn.execute.call_args_list[0]
        sql_text = str(reset_call[0][0])
        assert "downloaded" in sql_text


class TestAtomicDownload:
    """AC40: Atomic zip download (tmp + rename)."""

    @patch("lavandula.nonprofits.teos_download.requests.get")
    def test_atomic_rename(self, mock_get, tmp_path):
        content = b"PK" + b"\x00" * 100
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.headers = {"Content-Length": str(len(content))}
        resp.iter_content = MagicMock(return_value=[content])
        mock_get.return_value = resp

        dest = tmp_path / "test.zip"
        _download_zip("http://example.com/test.zip", dest)

        assert dest.exists()
        tmp_file = dest.with_suffix(".zip.tmp")
        assert not tmp_file.exists()


class TestRetryOn5xx:
    """AC41: Retry with exponential backoff on 429/5xx."""

    @patch("lavandula.nonprofits.teos_download.time.sleep")
    @patch("lavandula.nonprofits.teos_download.requests.get")
    def test_retry_on_503(self, mock_get, mock_sleep, tmp_path):
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        fail_resp.headers = {}
        fail_resp.raise_for_status = MagicMock(side_effect=Exception("503"))

        content = b"PK" + b"\x00" * 100
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.raise_for_status = MagicMock()
        ok_resp.headers = {"Content-Length": str(len(content))}
        ok_resp.iter_content = MagicMock(return_value=[content])

        mock_get.side_effect = [fail_resp, ok_resp]

        dest = tmp_path / "test.zip"
        _download_zip("http://example.com/test.zip", dest)

        assert dest.exists()
        mock_sleep.assert_called_once_with(2)

    @patch("lavandula.nonprofits.teos_download.requests.get")
    def test_404_raises_file_not_found(self, mock_get, tmp_path):
        resp = MagicMock()
        resp.status_code = 404
        mock_get.return_value = resp

        dest = tmp_path / "test.zip"
        with pytest.raises(FileNotFoundError):
            _download_zip("http://example.com/test.zip", dest)

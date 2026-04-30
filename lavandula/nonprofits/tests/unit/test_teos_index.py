"""Unit tests for teos_index.py (Spec 0026)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lavandula.nonprofits.teos_index import IndexStats, download_and_filter_index


def _mock_engine_with_eins(eins: list[str]):
    engine = MagicMock()
    conn = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value = ctx

    result = MagicMock()
    result.fetchall.return_value = [(e,) for e in eins]
    conn.execute.return_value = result

    begin_conn = MagicMock()
    begin_ctx = MagicMock()
    begin_ctx.__enter__ = MagicMock(return_value=begin_conn)
    begin_ctx.__exit__ = MagicMock(return_value=False)
    engine.begin.return_value = begin_ctx

    insert_result = MagicMock()
    insert_result.rowcount = 1
    begin_conn.execute.return_value = insert_result

    return engine, begin_conn


def _make_csv_response(rows: list[str]) -> MagicMock:
    header = "RETURN_ID,FILING_TYPE,EIN,TAX_PERIOD,SUB_DATE,TAXPAYER_NAME,RETURN_TYPE,DLN,OBJECT_ID,XML_BATCH_ID"
    content = (header + "\n" + "\n".join(rows)).encode("utf-8")

    from io import BytesIO
    resp = MagicMock()
    resp.raw = BytesIO(content)
    resp.raise_for_status = MagicMock()
    return resp


class TestDownloadAndFilterIndex:
    @patch("lavandula.nonprofits.teos_index.requests.get")
    def test_filters_return_type(self, mock_get):
        """AC6: filters to RETURN_TYPE='990'."""
        engine, conn = _mock_engine_with_eins(["123456789"])
        mock_get.return_value = _make_csv_response([
            "1,EFILE,123456789,202312,2024,ORG A,990,DLN1,OBJ001,2024_TEOS_XML_01A",
            "2,EFILE,123456789,202312,2024,ORG A,990EZ,DLN2,OBJ002,2024_TEOS_XML_01A",
            "3,EFILE,123456789,202312,2024,ORG A,990PF,DLN3,OBJ003,2024_TEOS_XML_01A",
        ])

        stats = download_and_filter_index(
            engine=engine, year=2024, ein="123456789",
        )

        assert stats.rows_matched == 1
        assert stats.rows_inserted == 1

    @patch("lavandula.nonprofits.teos_index.requests.get")
    def test_filters_to_matching_eins(self, mock_get):
        """AC6: filters to EINs in nonprofits_seed."""
        engine, conn = _mock_engine_with_eins(["111111111"])
        mock_get.return_value = _make_csv_response([
            "1,EFILE,111111111,202312,2024,OUR ORG,990,DLN1,OBJ001,2024_TEOS_XML_01A",
            "2,EFILE,222222222,202312,2024,OTHER ORG,990,DLN2,OBJ002,2024_TEOS_XML_01A",
        ])

        stats = download_and_filter_index(
            engine=engine, year=2024, state="NY",
        )

        assert stats.rows_matched == 1

    @patch("lavandula.nonprofits.teos_index.requests.get")
    def test_idempotent_insertion(self, mock_get):
        """AC52: running same year twice doesn't duplicate filing_index rows."""
        engine, conn = _mock_engine_with_eins(["123456789"])
        csv_row = "1,EFILE,123456789,202312,2024,ORG A,990,DLN1,OBJ001,2024_TEOS_XML_01A"

        # First run: rowcount=1 (inserted)
        insert_result = MagicMock()
        insert_result.rowcount = 1
        conn.execute.return_value = insert_result
        mock_get.return_value = _make_csv_response([csv_row])
        stats1 = download_and_filter_index(
            engine=engine, year=2024, ein="123456789",
        )
        assert stats1.rows_inserted == 1

        # Second run: rowcount=0 (skipped by ON CONFLICT DO NOTHING)
        insert_result2 = MagicMock()
        insert_result2.rowcount = 0
        conn.execute.return_value = insert_result2
        mock_get.return_value = _make_csv_response([csv_row])
        stats2 = download_and_filter_index(
            engine=engine, year=2024, ein="123456789",
        )
        assert stats2.rows_inserted == 0
        assert stats2.rows_skipped == 1

    def test_requires_state_or_ein(self):
        """CLI requires --state or --ein."""
        engine = MagicMock()
        with pytest.raises(ValueError, match="Must specify"):
            download_and_filter_index(engine=engine, year=2024)

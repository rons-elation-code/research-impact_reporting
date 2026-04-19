"""AC22 — hard delete round-trip; AC22.1 — retention sweep."""
from __future__ import annotations

import pytest


def test_ac22_delete_round_trip(tmp_reports_db, tmp_archive_dir):
    from lavandula.reports.catalogue import delete
    from lavandula.reports.schema import insert_raw_report_for_test

    sha = "a" * 64
    insert_raw_report_for_test(
        tmp_reports_db,
        content_sha256=sha,
        source_org_ein="000000001",
        attribution_confidence="own_domain",
    )
    pdf_path = tmp_archive_dir / f"{sha}.pdf"
    pdf_path.write_bytes(b"%PDF-1.7")
    pdf_path.chmod(0o600)

    delete(
        tmp_reports_db,
        content_sha256=sha,
        reason="test",
        operator="tester",
        archive_dir=tmp_archive_dir,
    )
    assert not pdf_path.exists()
    rows = list(tmp_reports_db.execute(
        "SELECT content_sha256 FROM reports WHERE content_sha256 = ?", (sha,)
    ))
    assert rows == []
    dlog = list(tmp_reports_db.execute(
        "SELECT content_sha256, pdf_unlinked, reason FROM deletion_log WHERE content_sha256 = ?",
        (sha,),
    ))
    assert len(dlog) == 1
    assert dlog[0][0] == sha
    assert dlog[0][1] == 1


def test_ac22_1_retention_sweep(tmp_reports_db, tmp_archive_dir):
    from lavandula.reports.catalogue import sweep_stale
    from lavandula.reports.schema import insert_raw_report_for_test
    recent = "2026-04-19T00:00:00Z"
    ancient = "2024-01-01T00:00:00Z"

    # 2 recent, 3 ancient
    for i, at in enumerate([recent, recent, ancient, ancient, ancient]):
        sha = f"{i}" * 64
        insert_raw_report_for_test(
            tmp_reports_db,
            content_sha256=sha,
            source_org_ein=f"00000000{i+1}",
            archived_at=at,
            attribution_confidence="own_domain",
        )
        (tmp_archive_dir / f"{sha}.pdf").write_bytes(b"%PDF-1.7")

    deleted = sweep_stale(
        tmp_reports_db,
        now_iso="2026-04-19T00:00:00Z",
        retention_days=365,
        archive_dir=tmp_archive_dir,
    )
    assert deleted == 3
    survivors = list(tmp_reports_db.execute("SELECT COUNT(*) FROM reports"))
    assert survivors[0][0] == 2
    dlog = list(tmp_reports_db.execute(
        "SELECT COUNT(*) FROM deletion_log WHERE reason = 'retention_expired'"
    ))
    assert dlog[0][0] == 3

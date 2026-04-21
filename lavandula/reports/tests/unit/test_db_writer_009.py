from __future__ import annotations


def _write_report(conn, **overrides):
    from lavandula.reports import db_writer

    base = dict(
        content_sha256="f" * 64,
        source_url_redacted="https://example.org/report.pdf",
        referring_page_url_redacted="https://example.org/reports",
        redirect_chain_redacted=["https://example.org/report.pdf"],
        source_org_ein="000000001",
        discovered_via="homepage-link",
        hosting_platform="own-domain",
        attribution_confidence="platform_unverified",
        file_size_bytes=1024,
        page_count=None,
        first_page_text=None,
        pdf_creator=None,
        pdf_producer=None,
        pdf_creation_date=None,
        pdf_has_javascript=0,
        pdf_has_launch=0,
        pdf_has_embedded=0,
        pdf_has_uri_actions=0,
        classification=None,
        classification_confidence=None,
        classifier_model="claude-haiku-4-5",
        classifier_version=1,
        report_year=None,
        report_year_source=None,
        extractor_version=1,
    )
    base.update(overrides)
    db_writer.upsert_report(conn, **base)


def test_upsert_report_upgrades_stronger_attribution(tmp_reports_db):
    _write_report(
        tmp_reports_db,
        source_org_ein="000000001",
        source_url_redacted="https://directory.example/report.pdf",
        attribution_confidence="platform_unverified",
    )
    _write_report(
        tmp_reports_db,
        source_org_ein="000000999",
        source_url_redacted="https://official.example/report.pdf",
        attribution_confidence="own_domain",
    )

    row = tmp_reports_db.execute(
        "SELECT source_org_ein, source_url_redacted, attribution_confidence "
        "FROM reports WHERE content_sha256 = ?",
        ("f" * 64,),
    ).fetchone()
    assert row["source_org_ein"] == "000000999"
    assert row["source_url_redacted"] == "https://official.example/report.pdf"
    assert row["attribution_confidence"] == "own_domain"


def test_upsert_report_rejects_weaker_attribution_downgrade(tmp_reports_db):
    _write_report(
        tmp_reports_db,
        source_org_ein="000000111",
        source_url_redacted="https://official.example/report.pdf",
        attribution_confidence="own_domain",
    )
    _write_report(
        tmp_reports_db,
        source_org_ein="000000222",
        source_url_redacted="https://platform.example/report.pdf",
        attribution_confidence="platform_unverified",
    )

    row = tmp_reports_db.execute(
        "SELECT source_org_ein, source_url_redacted, attribution_confidence "
        "FROM reports WHERE content_sha256 = ?",
        ("f" * 64,),
    ).fetchone()
    assert row["source_org_ein"] == "000000111"
    assert row["source_url_redacted"] == "https://official.example/report.pdf"
    assert row["attribution_confidence"] == "own_domain"


def test_upsert_report_backfills_missing_fields_and_better_classification(tmp_reports_db):
    _write_report(
        tmp_reports_db,
        page_count=None,
        first_page_text=None,
        classification="other",
        classification_confidence=0.55,
    )
    _write_report(
        tmp_reports_db,
        page_count=12,
        first_page_text="Impact Report",
        classification="impact",
        classification_confidence=0.95,
        report_year=2024,
        report_year_source="filename",
    )

    row = tmp_reports_db.execute(
        "SELECT page_count, first_page_text, classification, classification_confidence, "
        "report_year, report_year_source FROM reports WHERE content_sha256 = ?",
        ("f" * 64,),
    ).fetchone()
    assert row["page_count"] == 12
    assert row["first_page_text"] == "Impact Report"
    assert row["classification"] == "impact"
    assert row["classification_confidence"] == 0.95
    assert row["report_year"] == 2024
    assert row["report_year_source"] == "filename"

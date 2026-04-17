"""AC31: coverage_report.md generation."""
from lavandula.nonprofits import report, schema, db_writer
from lavandula.nonprofits.extract import ExtractedProfile


def _profile(ein, **kw):
    base = dict(
        ein=ein, name=f"Org {ein}",
        website_url="https://example.org",
        website_url_raw="https://example.org",
        rating_stars=4, overall_score=90.0, rated=1,
        total_revenue=1_000_000, total_expenses=900_000,
        program_expense_pct=85.0, state="NY", ntee_major="A",
        parse_status="ok",
    )
    base.update(kw)
    return ExtractedProfile(**base)


def test_report_generates_sections(tmp_path):
    db = tmp_path / "x.db"
    conn = schema.ensure_db(db)
    for i in range(10):
        ein = str(100000000 + i)
        db_writer.upsert_nonprofit(
            conn, _profile(ein),
            cn_profile_url=f"https://www.charitynavigator.org/ein/{ein}",
            content_sha256="x",
        )
        db_writer.insert_fetch_log(
            conn, ein=ein, url="x", status_code=200, attempt=1,
            is_retry=False, fetch_status="ok", elapsed_ms=1, bytes_read=1000,
        )
        db_writer.insert_sitemap_entry(
            conn, ein=ein, source_sitemap="Sitemap1.xml",
        )
    conn.close()
    text = report.generate(db)
    assert "Totals" in text
    assert "EINs enumerated" in text
    assert "Field population" in text
    assert "Top 20 states" in text
    assert "Rating distribution" in text

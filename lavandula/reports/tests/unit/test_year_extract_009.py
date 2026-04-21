from __future__ import annotations


def test_infer_report_year_from_filename():
    from lavandula.reports.year_extract import infer_report_year

    year, source = infer_report_year(
        source_url="https://example.org/reports/2023-annual-report.pdf",
        first_page_text=None,
        pdf_creation_date=None,
    )
    assert (year, source) == (2023, "filename")


def test_infer_report_year_from_url_path():
    from lavandula.reports.year_extract import infer_report_year

    year, source = infer_report_year(
        source_url="https://example.org/annual/2022/report.pdf",
        first_page_text=None,
        pdf_creation_date=None,
    )
    assert (year, source) == (2022, "url")


def test_infer_report_year_from_first_page_text():
    from lavandula.reports.year_extract import infer_report_year

    year, source = infer_report_year(
        source_url="https://example.org/report.pdf",
        first_page_text="2024 Annual Impact Report",
        pdf_creation_date=None,
    )
    assert (year, source) == (2024, "first-page")


def test_infer_report_year_from_creation_date():
    from lavandula.reports.year_extract import infer_report_year

    year, source = infer_report_year(
        source_url="https://example.org/report.pdf",
        first_page_text=None,
        pdf_creation_date="D:20210101000000Z",
    )
    assert (year, source) == (2021, "pdf-creation-date")

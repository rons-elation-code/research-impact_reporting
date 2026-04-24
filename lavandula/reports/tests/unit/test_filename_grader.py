"""Unit tests for filename heuristic grading (sub-phase 1.4, AC06)."""
from __future__ import annotations

from pathlib import Path

import pytest

from lavandula.reports.filename_grader import grade_filename, normalize
from lavandula.reports.taxonomy import load_taxonomy

YAML_PATH = Path(__file__).parents[3] / "docs" / "collateral_taxonomy.yaml"


@pytest.fixture(scope="module")
def tax():
    return load_taxonomy(YAML_PATH)


# --- Known-good fixtures (accept tier, AC06) ---


def test_accept_uhs_annual_report(tax):
    assert grade_filename("UHS-Foundation-Annual-Report-2018.pdf", tax) >= 0.8


def test_accept_hss_annual_report(tax):
    assert grade_filename("HSS-Annual-Report-2024.pdf", tax) >= 0.8


def test_accept_carnegie_annual_report(tax):
    assert grade_filename("Carnegie-Council-Annual-Report-2021-300dpi.pdf", tax) >= 0.8


# --- Known-junk fixtures (reject tier, AC06) ---


def test_reject_ram_coloring_page(tax):
    assert grade_filename("Ram_Coloring_Page.pdf", tax) <= 0.2


def test_reject_wfh_permission_guidelines(tax):
    assert grade_filename("WFH-Permission_guidelines.pdf", tax) <= 0.2


def test_reject_waiver_substitution_form(tax):
    assert grade_filename("Waiver-Substitution-Form.pdf", tax) <= 0.2


# --- Neutral fixtures (middle tier, AC06) ---


def test_neutral_download_basename(tax):
    assert grade_filename("download", tax) == 0.5


def test_neutral_document_number(tax):
    assert grade_filename("document-1234.pdf", tax) == 0.5


# --- Year bonus ---


def test_year_bonus_present(tax):
    assert grade_filename("report-2024.pdf", tax) > grade_filename("report.pdf", tax)


def test_fy_year_bonus(tax):
    score = grade_filename("fy24-results.pdf", tax)
    assert score > 0.5


def test_pre_2000_year_bonus(tax):
    score = grade_filename("report-1998.pdf", tax)
    assert score > 0.5


# --- Normalize ---


def test_normalize_strips_pdf_lowercases():
    assert normalize("Annual_Report 2024.pdf") == "annual-report-2024"


def test_normalize_no_extension():
    assert normalize("download") == "download"

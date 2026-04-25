"""Tests for wayback_validation.py (AC15.2, AC15.3, AC25.2, AC25.3)."""
from __future__ import annotations

import pytest

from lavandula.reports.wayback_validation import (
    build_cdx_url,
    build_wayback_url,
    validate_cdx_row,
    validate_domain,
)


# ---------- validate_domain (AC15.2, AC25.2) ----------

class TestValidateDomain:
    def test_valid_simple(self):
        assert validate_domain("sloan.org") == "sloan.org"

    def test_valid_subdomain(self):
        assert validate_domain("reports.sloan.org") == "reports.sloan.org"

    def test_uppercased(self):
        assert validate_domain("SLOAN.ORG") == "sloan.org"

    def test_rejects_empty(self):
        assert validate_domain("") is None

    def test_rejects_query_param_injection(self):
        assert validate_domain("evil.org&matchType=exact&filter=") is None

    def test_rejects_slash(self):
        assert validate_domain("evil.org/") is None

    def test_rejects_question_mark(self):
        assert validate_domain("evil.org?x=y") is None

    def test_rejects_hash(self):
        assert validate_domain("evil.org#frag") is None

    def test_rejects_dots_only(self):
        assert validate_domain("..") is None

    def test_rejects_single_label(self):
        assert validate_domain("localhost") is None

    def test_rejects_oversized(self):
        assert validate_domain("a" * 254 + ".org") is None

    def test_rejects_control_chars(self):
        assert validate_domain("evil\x00.org") is None

    def test_rejects_space(self):
        assert validate_domain("evil .org") is None

    def test_rejects_underscore(self):
        assert validate_domain("evil_host.org") is None

    def test_valid_hyphenated(self):
        assert validate_domain("my-nonprofit.org") == "my-nonprofit.org"

    def test_rejects_leading_hyphen(self):
        assert validate_domain("-evil.org") is None

    def test_rejects_trailing_hyphen(self):
        assert validate_domain("evil-.org") is None


# ---------- validate_cdx_row (AC15.3, AC25.3) ----------

class TestValidateCdxRow:
    def test_valid_row(self):
        row = [
            "org,sloan)/annual-report.pdf",
            "20260406121250",
            "https://sloan.org/annual-report.pdf",
            "application/pdf",
            "200",
            "ABCDEF1234567890",
            "12345",
        ]
        result = validate_cdx_row(row)
        assert result is not None
        assert result["timestamp"] == "20260406121250"
        assert result["original"] == "https://sloan.org/annual-report.pdf"
        assert result["capture_host"] == "sloan.org"
        assert result["digest"] == "ABCDEF1234567890"

    def test_short_row_3_columns(self):
        row = ["urlkey", "20260406121250", "https://sloan.org/f.pdf"]
        result = validate_cdx_row(row)
        assert result is not None
        assert result["digest"] is None

    def test_rejects_too_short(self):
        assert validate_cdx_row(["urlkey", "ts"]) is None

    def test_rejects_non_list(self):
        assert validate_cdx_row("not a list") is None

    def test_rejects_bad_timestamp_traversal(self):
        assert validate_cdx_row(["uk", "../../etc", "https://x.org/f.pdf"]) is None

    def test_rejects_bad_timestamp_short(self):
        assert validate_cdx_row(["uk", "2024", "https://x.org/f.pdf"]) is None

    def test_rejects_javascript_scheme(self):
        assert validate_cdx_row(["uk", "20260406121250", "javascript:alert(1)"]) is None

    def test_rejects_data_scheme(self):
        assert validate_cdx_row(["uk", "20260406121250", "data:text/html,<h1>hi</h1>"]) is None

    def test_rejects_ftp_scheme(self):
        assert validate_cdx_row(["uk", "20260406121250", "ftp://x.org/f.pdf"]) is None

    def test_rejects_header_injection(self):
        assert validate_cdx_row(["uk", "20260406121250", "http://x.com\r\nHost: evil"]) is None

    def test_rejects_oversized_url(self):
        long_url = "https://example.org/" + "a" * 2100
        assert validate_cdx_row(["uk", "20260406121250", long_url]) is None

    def test_strips_credentials(self):
        row = ["uk", "20260406121250", "https://user:pw@host.org/x.pdf"]
        result = validate_cdx_row(row)
        assert result is not None
        assert "user" not in result["original"]
        assert "pw" not in result["original"]
        assert result["original"] == "https://host.org/x.pdf"

    def test_strips_fragment(self):
        row = ["uk", "20260406121250", "https://host.org/x.pdf#chapter1"]
        result = validate_cdx_row(row)
        assert result is not None
        assert "#" not in result["original"]

    def test_extra_columns_ignored(self):
        row = ["uk", "20260406121250", "https://x.org/f.pdf", "a", "b", "c", "d", "e", "extra"]
        result = validate_cdx_row(row)
        assert result is not None

    def test_rejects_no_hostname(self):
        assert validate_cdx_row(["uk", "20260406121250", "https:///path"]) is None

    def test_preserves_query_string(self):
        row = ["uk", "20260406121250", "https://x.org/f.pdf?v=2"]
        result = validate_cdx_row(row)
        assert result is not None
        assert "?v=2" in result["original"]


# ---------- build_wayback_url (AC8) ----------

class TestBuildWaybackUrl:
    def test_basic(self):
        url = build_wayback_url("20260406121250", "https://sloan.org/report.pdf")
        assert url == "https://web.archive.org/web/20260406121250id_/https://sloan.org/report.pdf"

    def test_special_chars_escaped(self):
        url = build_wayback_url("20260406121250", "https://example.org/r e port.pdf")
        assert "r%20e%20port.pdf" in url


# ---------- build_cdx_url (AC15.2) ----------

class TestBuildCdxUrl:
    def test_valid_domain(self):
        url = build_cdx_url("sloan.org")
        assert url is not None
        assert "url=sloan.org/*" in url
        assert "matchType=domain" in url
        assert "filter=mimetype:application/pdf" in url
        assert "limit=500" in url

    def test_rejects_malicious_domain(self):
        assert build_cdx_url("evil.org&matchType=exact&filter=") is None

    def test_rejects_empty(self):
        assert build_cdx_url("") is None

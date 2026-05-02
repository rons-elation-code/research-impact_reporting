"""Unit tests for phone_extract.py (Spec 0031)."""
from __future__ import annotations

import pytest

from lavandula.nonprofits.phone_extract import extract_phone


class TestExtractPhone:
    def test_standard_format(self):
        assert extract_phone("Call us at (555) 123-4567") == "(555) 123-4567"

    def test_dashed_format(self):
        assert extract_phone("Phone: 555-123-4567") == "(555) 123-4567"

    def test_dotted_format(self):
        assert extract_phone("555.123.4567") == "(555) 123-4567"

    def test_with_country_code(self):
        assert extract_phone("+1 555 123 4567") == "(555) 123-4567"

    def test_with_one_prefix_dash(self):
        assert extract_phone("1-555-123-4567") == "(555) 123-4567"

    def test_fax_rejected(self):
        assert extract_phone("Fax: (555) 123-4567") is None

    def test_fax_case_insensitive(self):
        assert extract_phone("fax number: 555-123-4567") is None

    def test_fax_within_20_chars(self):
        assert extract_phone("Send fax to this num 555-123-4567") is None

    def test_fax_beyond_20_chars_allowed(self):
        assert extract_phone("Our fax is available at the front desk. Call us at 555-123-4567") == "(555) 123-4567"

    def test_tollfree_rejected_default(self):
        assert extract_phone("Call 800-555-1234") is None
        assert extract_phone("Call 888-555-1234") is None
        assert extract_phone("Call 877-555-1234") is None

    def test_tollfree_allowed(self):
        assert extract_phone("Call 800-555-1234", allow_tollfree=True) == "(800) 555-1234"

    def test_ein_not_matched(self):
        """9-digit EINs without separators should not be matched."""
        assert extract_phone("EIN: 123456789") is None

    def test_zipcode_not_matched(self):
        """ZIP+4 codes should not be matched."""
        assert extract_phone("ZIP: 12345-6789") is None

    def test_multiple_phones_first_returned(self):
        text = "Main: 555-111-2222, Sales: 555-333-4444"
        assert extract_phone(text) == "(555) 111-2222"

    def test_no_phone_returns_none(self):
        assert extract_phone("No phone number here") is None

    def test_empty_text(self):
        assert extract_phone("") is None

    def test_none_text(self):
        assert extract_phone("") is None

    def test_org_name_proximity(self):
        """When org_name provided, prefer phone closest to org name."""
        text = "General info: 555-111-2222. For other inquiries contact the main office. Acme Foundation: 555-333-4444."
        result = extract_phone(text, org_name="Acme Foundation")
        assert result == "(555) 333-4444"

    def test_fax_skipped_real_phone_found(self):
        """Fax number skipped, real phone returned."""
        text = "Fax: 555-111-2222, Phone: 555-333-4444"
        assert extract_phone(text) == "(555) 333-4444"

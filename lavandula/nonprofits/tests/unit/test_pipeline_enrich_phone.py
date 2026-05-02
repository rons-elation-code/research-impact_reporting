"""Integration test for phone enrichment pipeline (Spec 0031, AC 50)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lavandula.nonprofits.phone_extract import extract_phone


class TestPhoneEnrichmentFlow:
    """Test the phone enrichment logic without a real DB."""

    def test_phone_from_snippet(self):
        """Phone extracted from search snippet."""
        snippet = "Acme Nonprofit — Call us at (512) 555-1234 for more info."
        phone = extract_phone(snippet, org_name="Acme Nonprofit")
        assert phone == "(512) 555-1234"

    def test_phone_from_snippet_with_fax(self):
        """Fax skipped, phone extracted."""
        snippet = "Fax: (512) 555-0000 | Phone: (512) 555-1234"
        phone = extract_phone(snippet, org_name="Test Org")
        assert phone == "(512) 555-1234"

    def test_no_phone_in_snippet(self):
        """No phone in snippet returns None, would trigger website fallback."""
        snippet = "Acme Nonprofit is a 501(c)(3) organization."
        phone = extract_phone(snippet, org_name="Acme Nonprofit")
        assert phone is None

    def test_phone_from_website_text(self):
        """Phone extracted from website contact page text."""
        page_text = "Contact us at our office: 214-555-6789 or email info@acme.org"
        phone = extract_phone(page_text, org_name="Acme Nonprofit")
        assert phone == "(214) 555-6789"

    def test_tollfree_rejected_in_enrichment(self):
        """Toll-free numbers rejected by default."""
        snippet = "Call us at 800-555-1234"
        phone = extract_phone(snippet, org_name="Test Org")
        assert phone is None

    def test_tollfree_allowed_with_flag(self):
        """Toll-free allowed when flag is set."""
        snippet = "Call us at 800-555-1234"
        phone = extract_phone(snippet, org_name="Test Org", allow_tollfree=True)
        assert phone == "(800) 555-1234"

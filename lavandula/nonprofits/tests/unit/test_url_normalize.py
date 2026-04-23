"""Unit tests for url_normalize.py (Spec 0018)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from lavandula.nonprofits.url_normalize import normalize_url


class TestStripTracking:
    def test_strip_utm(self):
        result = normalize_url(
            "https://foo.org/?utm_source=x&page=1", check_https=False
        )
        assert "utm_source" not in result
        assert "page=1" in result

    def test_strip_fbclid(self):
        result = normalize_url(
            "https://foo.org/?fbclid=abc", check_https=False
        )
        assert "fbclid" not in result
        assert result == "https://foo.org/"

    def test_strip_gclid(self):
        result = normalize_url(
            "https://foo.org/page?gclid=xyz&id=5", check_https=False
        )
        assert "gclid" not in result
        assert "id=5" in result

    def test_strip_ref(self):
        result = normalize_url(
            "https://foo.org/?ref=bar", check_https=False
        )
        assert "ref" not in result


class TestTrailingSlash:
    def test_trailing_slash_bare_domain(self):
        result = normalize_url("https://foo.org", check_https=False)
        assert result == "https://foo.org/"

    def test_trailing_slash_bare_domain_with_slash(self):
        result = normalize_url("https://foo.org/", check_https=False)
        assert result == "https://foo.org/"

    def test_no_trailing_slash_path(self):
        result = normalize_url("https://foo.org/about/", check_https=False)
        assert result == "https://foo.org/about"

    def test_no_trailing_slash_deep_path(self):
        result = normalize_url("https://foo.org/about/team/", check_https=False)
        assert result == "https://foo.org/about/team"


class TestHttpsUpgrade:
    def test_https_upgrade(self):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.http_status = 200
        mock_client.head.return_value = mock_result

        with patch("lavandula.reports.http_client.ReportsHTTPClient", return_value=mock_client):
            result = normalize_url("http://foo.org/", check_https=True)

        assert result.startswith("https://")

    def test_no_upgrade_when_https_fails(self):
        mock_client = MagicMock()
        mock_client.head.side_effect = ConnectionError("refused")

        with patch("lavandula.reports.http_client.ReportsHTTPClient", return_value=mock_client):
            result = normalize_url("http://foo.org/", check_https=True)

        assert result.startswith("http://")

    def test_already_https_no_check(self):
        with patch("lavandula.reports.http_client.ReportsHTTPClient") as mock_cls:
            result = normalize_url("https://foo.org/", check_https=True)

        mock_cls.assert_not_called()
        assert result == "https://foo.org/"

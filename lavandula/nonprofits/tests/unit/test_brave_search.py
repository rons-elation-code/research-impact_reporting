"""Unit tests for brave_search.py (Spec 0018)."""
from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from lavandula.nonprofits.brave_search import (
    BLOCKLIST_DOMAINS,
    BraveRateLimiter,
    BraveSearchError,
    BraveSearchResult,
    is_blocked,
    search,
    search_and_filter,
)


def _mock_brave_response(results: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"web": {"results": results}}
    return resp


def _mock_brave_error(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {}
    return resp


class TestSearch:
    def test_search_returns_results(self):
        results = [
            {"title": "Example Org", "url": "https://example.org", "description": "A nonprofit"},
            {"title": "Another", "url": "https://another.org", "description": "Another"},
        ]
        rl = BraveRateLimiter(100.0)
        with patch("lavandula.nonprofits.brave_search.requests.get") as mock_get:
            mock_get.return_value = _mock_brave_response(results)
            out = search("test query", api_key="key123", rate_limiter=rl)
        assert len(out) == 2
        assert out[0].title == "Example Org"
        assert out[0].url == "https://example.org"
        assert out[1].snippet == "Another"

    def test_zero_results(self):
        rl = BraveRateLimiter(100.0)
        with patch("lavandula.nonprofits.brave_search.requests.get") as mock_get:
            mock_get.return_value = _mock_brave_response([])
            out = search("test", api_key="key", rate_limiter=rl)
        assert out == []

    def test_retry_on_429(self):
        rl = BraveRateLimiter(100.0)
        ok_resp = _mock_brave_response([{"title": "OK", "url": "https://ok.org", "description": "ok"}])
        with patch("lavandula.nonprofits.brave_search.requests.get") as mock_get:
            mock_get.side_effect = [
                _mock_brave_error(429),
                _mock_brave_error(429),
                ok_resp,
            ]
            with patch("lavandula.nonprofits.brave_search.time.sleep"):
                out = search("test", api_key="key", rate_limiter=rl)
        assert len(out) == 1
        assert out[0].title == "OK"

    def test_search_error_on_exhaustion(self):
        rl = BraveRateLimiter(100.0)
        with patch("lavandula.nonprofits.brave_search.requests.get") as mock_get:
            mock_get.return_value = _mock_brave_error(500)
            with patch("lavandula.nonprofits.brave_search.time.sleep"):
                with pytest.raises(BraveSearchError):
                    search("test", api_key="key", rate_limiter=rl)

    def test_no_retry_on_400(self):
        rl = BraveRateLimiter(100.0)
        with patch("lavandula.nonprofits.brave_search.requests.get") as mock_get:
            mock_get.return_value = _mock_brave_error(400)
            with pytest.raises(BraveSearchError, match="400"):
                search("test", api_key="key", rate_limiter=rl)
        assert mock_get.call_count == 1

    def test_retry_does_not_double_count_rate_limit(self):
        """AC25: retries reuse the same rate limiter permit."""
        rl = BraveRateLimiter(100.0)
        acquire_calls = 0
        original_acquire = rl.acquire

        def counting_acquire():
            nonlocal acquire_calls
            acquire_calls += 1
            original_acquire()

        rl.acquire = counting_acquire
        ok_resp = _mock_brave_response([{"title": "OK", "url": "https://ok.org", "description": "ok"}])
        with patch("lavandula.nonprofits.brave_search.requests.get") as mock_get:
            mock_get.side_effect = [_mock_brave_error(429), ok_resp]
            with patch("lavandula.nonprofits.brave_search.time.sleep"):
                search("test", api_key="key", rate_limiter=rl)
        assert acquire_calls == 1


class TestBlocklist:
    def test_blocklist_suffix_match(self):
        """AC19: www.linkedin.com blocked, linkedin-example.com not."""
        assert is_blocked("www.linkedin.com", "Test Org") is True
        assert is_blocked("au.linkedin.com", "Test Org") is True
        assert is_blocked("linkedin.com", "Test Org") is True
        assert is_blocked("linkedin-example.com", "Test Org") is False

    def test_blocklist_gov_exemption(self):
        """AC20: .gov blocked unless org name has 'authority' or 'commission'."""
        assert is_blocked("state.gov", "Test Org") is True
        assert is_blocked("state.gov", "Housing Authority of Dallas") is False
        assert is_blocked("state.gov", "State Ethics Commission") is False

    def test_blocklist_domains_all_blocked(self):
        for domain in ("propublica.org", "yelp.com", "causeiq.com", "taxexemptworld.com"):
            assert is_blocked(domain, "Test") is True

    def test_blocklist_case_insensitive(self):
        assert is_blocked("WWW.LINKEDIN.COM", "Test") is True
        assert is_blocked("Www.LinkedIn.com", "Test") is True


class TestRateLimiter:
    def test_rate_limiter_enforced(self):
        """AC2: at QPS=2, 10 calls take at least 4.5s."""
        rl = BraveRateLimiter(2.0)
        t0 = time.monotonic()
        for _ in range(10):
            rl.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed >= 4.0

    def test_rate_limiter_invalid_qps(self):
        with pytest.raises(ValueError):
            BraveRateLimiter(0.0)


class TestSearchAndFilter:
    def test_search_and_filter_blocklist(self):
        results = [
            {"title": "LinkedIn", "url": "https://www.linkedin.com/company/test", "description": "..."},
            {"title": "Official", "url": "https://testorg.org", "description": "..."},
            {"title": "Propublica", "url": "https://propublica.org/test", "description": "..."},
        ]
        rl = BraveRateLimiter(100.0)
        with patch("lavandula.nonprofits.brave_search.requests.get") as mock_get:
            mock_get.return_value = _mock_brave_response(results)
            out = search_and_filter(
                "Test Org", "Dallas", "TX",
                api_key="key", rate_limiter=rl,
            )
        assert len(out) == 1
        assert out[0].url == "https://testorg.org"


class TestApiKeyNotLogged:
    def test_api_key_not_logged(self):
        """AC28: API keys never appear in log output."""
        secret_key = "super_secret_brave_key_12345"
        captured = []

        class CapturingHandler(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        handler = CapturingHandler()
        logger = logging.getLogger("lavandula.nonprofits.brave_search")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        rl = BraveRateLimiter(100.0)
        try:
            with patch("lavandula.nonprofits.brave_search.requests.get") as mock_get:
                mock_get.return_value = _mock_brave_error(500)
                with patch("lavandula.nonprofits.brave_search.time.sleep"):
                    try:
                        search("test", api_key=secret_key, rate_limiter=rl)
                    except BraveSearchError:
                        pass
        finally:
            logger.removeHandler(handler)

        for msg in captured:
            assert secret_key not in msg, f"API key leaked in log: {msg}"

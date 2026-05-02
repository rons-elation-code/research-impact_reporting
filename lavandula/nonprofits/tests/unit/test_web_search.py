"""Unit tests for web_search.py (Spec 0031)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from lavandula.nonprofits.web_search import (
    RateLimiter,
    SearchConfig,
    SearchError,
    SearchFilterResult,
    SearchResult,
    SearchStats,
    _merge_results,
    _normalize_url,
    _serpex_search,
    reset_search_stats,
    search,
    search_and_filter,
    validate_engines,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_serpex_response(results: list[dict], status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"results": results}
    return resp


def _mock_serpex_error(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {}
    return resp


def _make_config(**overrides) -> SearchConfig:
    defaults = {
        "backend": "serpex",
        "engines": ["brave"],
        "api_key": "test-key",
        "qps": 100.0,
        "count": 10,
    }
    defaults.update(overrides)
    return SearchConfig(**defaults)


def _fast_rl() -> RateLimiter:
    return RateLimiter(1000.0)


# ── URL Normalization (AC 44) ────────────────────────────────────────────────


class TestNormalizeUrl:
    def test_strip_www(self):
        key, _ = _normalize_url("https://www.example.com/page")
        assert key == "example.com/page"

    def test_strip_trailing_slash(self):
        key, _ = _normalize_url("https://example.com/page/")
        assert key == "example.com/page"

    def test_root_path_stripped(self):
        key, _ = _normalize_url("https://example.com/")
        assert key == "example.com"

    def test_strip_fragment(self):
        key, _ = _normalize_url("https://example.com/page#section")
        assert key == "example.com/page"

    def test_preserve_query_string(self):
        key, _ = _normalize_url("https://example.com/page?id=1&sort=asc")
        assert key == "example.com/page?id=1&sort=asc"

    def test_preserve_path_case(self):
        key1, _ = _normalize_url("https://example.com/About")
        key2, _ = _normalize_url("https://example.com/about")
        assert key1 != key2

    def test_scheme_collapsed(self):
        key_http, _ = _normalize_url("http://example.com/page")
        key_https, _ = _normalize_url("https://example.com/page")
        assert key_http == key_https

    def test_lowercase_hostname(self):
        key, _ = _normalize_url("https://EXAMPLE.COM/Page")
        assert key == "example.com/Page"

    def test_remove_default_port_80(self):
        key, _ = _normalize_url("http://example.com:80/page")
        assert key == "example.com/page"

    def test_remove_default_port_443(self):
        key, _ = _normalize_url("https://example.com:443/page")
        assert key == "example.com/page"

    def test_preserve_non_default_port(self):
        key, _ = _normalize_url("https://example.com:8080/page")
        assert key == "example.com:8080/page"


# ── Engine Validation (AC 46) ────────────────────────────────────────────────


class TestValidateEngines:
    def test_valid_single(self):
        assert validate_engines(["brave"]) == ["brave"]

    def test_valid_multi(self):
        assert validate_engines(["brave", "google"]) == ["brave", "google"]

    def test_auto_alone(self):
        assert validate_engines(["auto"]) == ["auto"]

    def test_auto_combined_rejected(self):
        with pytest.raises(ValueError, match="auto"):
            validate_engines(["auto", "brave"])

    def test_unknown_engine_rejected(self):
        with pytest.raises(ValueError, match="potato"):
            validate_engines(["potato"])

    def test_duplicates_deduped(self):
        assert validate_engines(["brave", "brave"]) == ["brave"]

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="At least one"):
            validate_engines([])

    def test_whitespace_stripped(self):
        assert validate_engines([" brave ", " google "]) == ["brave", "google"]


# ── Serpex API Call (AC 42) ──────────────────────────────────────────────────


class TestSerpexSearch:
    def test_api_call_construction(self):
        results = [
            {"title": "Example", "url": "https://example.org", "snippet": "A nonprofit"},
        ]
        rl = _fast_rl()
        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.return_value = _mock_serpex_response(results)
            out = _serpex_search("test query", "brave", api_key="key123", count=10, rate_limiter=rl)

        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        assert call_kwargs.kwargs["headers"] == {"X-API-Key": "key123"}
        assert call_kwargs.kwargs["params"]["q"] == "test query"
        assert call_kwargs.kwargs["params"]["engine"] == "brave"
        assert call_kwargs.kwargs["params"]["category"] == "web"
        assert len(out) == 1
        assert out[0].title == "Example"
        assert out[0].engines == ("brave",)

    def test_response_parsing(self):
        results = [
            {"title": "T1", "url": "https://a.org", "snippet": "S1"},
            {"title": "T2", "url": "https://b.org", "snippet": "S2"},
        ]
        rl = _fast_rl()
        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.return_value = _mock_serpex_response(results)
            out = _serpex_search("q", "brave", api_key="k", count=10, rate_limiter=rl)

        assert len(out) == 2
        assert out[0].url == "https://a.org"
        assert out[1].snippet == "S2"


# ── Error Handling (AC 45) ──────────────────────────────────────────────────


class TestErrorHandling:
    def test_402_no_retry(self):
        rl = _fast_rl()
        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.return_value = _mock_serpex_error(402)
            with pytest.raises(SearchError, match="402"):
                _serpex_search("q", "brave", api_key="k", count=10, rate_limiter=rl)
        assert mock_get.call_count == 1

    def test_429_retried(self):
        rl = _fast_rl()
        ok_resp = _mock_serpex_response([{"title": "OK", "url": "https://ok.org", "snippet": "ok"}])
        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.side_effect = [_mock_serpex_error(429), _mock_serpex_error(429), ok_resp]
            with patch("lavandula.nonprofits.web_search.time.sleep"):
                out = _serpex_search("q", "brave", api_key="k", count=10, rate_limiter=rl)
        assert len(out) == 1

    def test_5xx_retried(self):
        rl = _fast_rl()
        ok_resp = _mock_serpex_response([{"title": "OK", "url": "https://ok.org", "snippet": "ok"}])
        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.side_effect = [_mock_serpex_error(500), ok_resp]
            with patch("lavandula.nonprofits.web_search.time.sleep"):
                out = _serpex_search("q", "brave", api_key="k", count=10, rate_limiter=rl)
        assert len(out) == 1

    def test_network_error_retried(self):
        import requests
        rl = _fast_rl()
        ok_resp = _mock_serpex_response([{"title": "OK", "url": "https://ok.org", "snippet": "ok"}])
        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.side_effect = [requests.Timeout("timeout"), ok_resp]
            with patch("lavandula.nonprofits.web_search.time.sleep"):
                out = _serpex_search("q", "brave", api_key="k", count=10, rate_limiter=rl)
        assert len(out) == 1

    def test_exhaustion_raises(self):
        rl = _fast_rl()
        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.return_value = _mock_serpex_error(500)
            with patch("lavandula.nonprofits.web_search.time.sleep"):
                with pytest.raises(SearchError):
                    _serpex_search("q", "brave", api_key="k", count=10, rate_limiter=rl)

    def test_multi_engine_partial_failure(self):
        reset_search_stats()
        rl = _fast_rl()
        ok_resp = _mock_serpex_response([{"title": "OK", "url": "https://ok.org", "snippet": "ok"}])
        config = _make_config(engines=["brave", "google"])

        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            # brave fails, google succeeds
            mock_get.side_effect = [_mock_serpex_error(500)] * 4 + [ok_resp]
            with patch("lavandula.nonprofits.web_search.time.sleep"):
                out = search("test", config=config, rate_limiter=rl)

        assert len(out) == 1
        assert "google" in out[0].engines

    def test_multi_engine_all_fail(self):
        reset_search_stats()
        rl = _fast_rl()
        config = _make_config(engines=["brave", "google"])

        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.return_value = _mock_serpex_error(500)
            with patch("lavandula.nonprofits.web_search.time.sleep"):
                with pytest.raises(SearchError, match="All engines failed"):
                    search("test", config=config, rate_limiter=rl)


# ── Multi-Engine Merge (AC 43) ──────────────────────────────────────────────


class TestMergeResults:
    def test_same_url_merged(self):
        results_by_engine = {
            "brave": [
                SearchResult("T1", "https://example.org", "S1", ("brave",)),
            ],
            "google": [
                SearchResult("T1b", "https://example.org", "S1b", ("google",)),
            ],
        }
        merged = _merge_results(results_by_engine)
        assert len(merged) == 1
        assert set(merged[0].engines) == {"brave", "google"}

    def test_multi_engine_sorts_first(self):
        results_by_engine = {
            "brave": [
                SearchResult("Brave Only", "https://brave-only.org", "S", ("brave",)),
                SearchResult("Both", "https://both.org", "S", ("brave",)),
            ],
            "google": [
                SearchResult("Both", "https://both.org", "S", ("google",)),
            ],
        }
        merged = _merge_results(results_by_engine)
        assert merged[0].url == "https://both.org"
        assert len(merged[0].engines) == 2

    def test_tiebreaker_by_rank(self):
        results_by_engine = {
            "brave": [
                SearchResult("A", "https://a.org", "S", ("brave",)),
                SearchResult("B", "https://b.org", "S", ("brave",)),
            ],
        }
        merged = _merge_results(results_by_engine)
        assert merged[0].url == "https://a.org"
        assert merged[1].url == "https://b.org"

    def test_tiebreaker_insertion_order(self):
        results_by_engine = {
            "brave": [
                SearchResult("A", "https://a.org", "S", ("brave",)),
            ],
            "google": [
                SearchResult("B", "https://b.org", "S", ("google",)),
            ],
        }
        merged = _merge_results(results_by_engine)
        # Both single-engine, rank 0. Brave inserted first (dict order).
        assert merged[0].url == "https://a.org"
        assert merged[1].url == "https://b.org"

    def test_https_preferred_over_http(self):
        results_by_engine = {
            "brave": [
                SearchResult("T1", "http://example.org", "S", ("brave",)),
            ],
            "google": [
                SearchResult("T1", "https://example.org", "S", ("google",)),
            ],
        }
        merged = _merge_results(results_by_engine)
        assert len(merged) == 1
        assert merged[0].url == "https://example.org"

    def test_www_deduped(self):
        results_by_engine = {
            "brave": [
                SearchResult("T1", "https://www.example.org/page", "S", ("brave",)),
            ],
            "google": [
                SearchResult("T1", "https://example.org/page", "S", ("google",)),
            ],
        }
        merged = _merge_results(results_by_engine)
        assert len(merged) == 1
        assert len(merged[0].engines) == 2


# ── search() Public API ─────────────────────────────────────────────────────


class TestSearch:
    def test_single_engine(self):
        reset_search_stats()
        rl = _fast_rl()
        config = _make_config(engines=["brave"])
        results = [{"title": "T", "url": "https://t.org", "snippet": "S"}]

        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.return_value = _mock_serpex_response(results)
            out = search("q", config=config, rate_limiter=rl)

        assert len(out) == 1
        assert out[0].engines == ("brave",)

    def test_brave_direct_backend(self):
        rl = _fast_rl()
        config = _make_config(backend="brave-direct")

        with patch("lavandula.nonprofits.brave_search.requests.get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"web": {"results": [
                {"title": "T", "url": "https://t.org", "description": "S"},
            ]}}
            mock_get.return_value = resp
            out = search("q", config=config, rate_limiter=rl)

        assert len(out) == 1
        assert out[0].engines == ("brave",)


# ── search_and_filter() (AC 47) ─────────────────────────────────────────────


class TestSearchAndFilter:
    def test_blocklist_applied(self):
        reset_search_stats()
        rl = _fast_rl()
        config = _make_config()
        results = [
            {"title": "LinkedIn", "url": "https://www.linkedin.com/company/test", "snippet": "..."},
            {"title": "Official", "url": "https://testorg.org", "snippet": "..."},
            {"title": "Propublica", "url": "https://propublica.org/test", "snippet": "..."},
            {"title": "Other", "url": "https://other.org", "snippet": "..."},
            {"title": "Third", "url": "https://third.org", "snippet": "..."},
        ]

        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.return_value = _mock_serpex_response(results)
            result = search_and_filter(
                "Test Org", "Dallas", "TX",
                config=config, rate_limiter=rl,
            )

        assert result.had_raw_results is True
        assert len(result.results) == 3
        assert result.results[0].url == "https://testorg.org"
        assert result.results[1].url == "https://other.org"
        assert result.results[2].url == "https://third.org"

    def test_empty_results(self):
        reset_search_stats()
        rl = _fast_rl()
        config = _make_config()

        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.return_value = _mock_serpex_response([])
            result = search_and_filter("Test", "City", "ST", config=config, rate_limiter=rl)

        assert result.results == []
        assert result.had_raw_results is False

    def test_all_blocked(self):
        reset_search_stats()
        rl = _fast_rl()
        config = _make_config()
        results = [
            {"title": "LinkedIn", "url": "https://linkedin.com/test", "snippet": "..."},
            {"title": "Propublica", "url": "https://propublica.org/test", "snippet": "..."},
        ]

        with patch("lavandula.nonprofits.web_search.requests.get") as mock_get:
            mock_get.return_value = _mock_serpex_response(results)
            result = search_and_filter("Test", "City", "ST", config=config, rate_limiter=rl)

        assert result.results == []
        assert result.had_raw_results is True


# ── SearchConfig repr (key not leaked) ───────────────────────────────────────


class TestSearchConfigRepr:
    def test_api_key_masked(self):
        config = _make_config(api_key="super_secret_key_12345")
        r = repr(config)
        assert "super_secret_key_12345" not in r
        assert "***" in r


# ── RateLimiter ──────────────────────────────────────────────────────────────


class TestRateLimiter:
    def test_rate_limiter_enforced(self):
        rl = RateLimiter(2.0)
        t0 = time.monotonic()
        for _ in range(10):
            rl.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed >= 4.0

    def test_rate_limiter_invalid_qps(self):
        with pytest.raises(ValueError):
            RateLimiter(0.0)

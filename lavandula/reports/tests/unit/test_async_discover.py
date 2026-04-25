"""Tests for async_discover.py (AC26, AC28, AC29, AC8 retry parity)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lavandula.reports.async_discover import (
    DiscoveryResult,
    discover_org,
)
from lavandula.reports.candidate_filter import Candidate
from lavandula.reports.discover import per_org_candidates


@dataclass
class _StubHTTPClient:
    responses: dict[str, tuple[bytes, str, int]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def get(self, url, *, kind="homepage", seed_etld1=None, extra_headers=None):
        self.calls.append((url, kind))
        body, status, http_status = self.responses.get(url, (b"", "network_error", 0))
        return _FakeResult(body=body, status=status, http_status=http_status,
                           final_url=url, final_url_redacted=url, note="")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


@dataclass
class _FakeResult:
    body: bytes | None = None
    status: str = "ok"
    http_status: int | None = 200
    final_url: str = ""
    final_url_redacted: str = ""
    note: str = ""
    redirect_chain: list[str] | None = None
    redirect_chain_redacted: list[str] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    kind: str = ""
    elapsed_ms: int = 0
    error: str | None = None
    bytes_read: int = 0


_MINIMAL_HTML = b"""<html><body>
<a href="https://example.com/report.pdf">Annual Report</a>
</body></html>"""

_PARITY_HTML = b"""<html><body>
<a href="https://example.com/2024-annual-report.pdf">2024 Annual Report</a>
<a href="https://example.com/financials/2023-statement.pdf">Financial Statement</a>
<a href="https://issuu.com/testorg/docs/impact-2024">Impact Report on Issuu</a>
<a href="https://example.com/about">About Us</a>
<a href="https://example.com/reports">Reports Page</a>
</body></html>"""

_PARITY_SUBPAGE_HTML = b"""<html><body>
<a href="https://example.com/uploads/full-impact-report.pdf">Full Impact Report</a>
</body></html>"""

_PARITY_SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/transparency/2024-audit.pdf</loc></url>
  <url><loc>https://example.com/year-in-review</loc></url>
</urlset>"""


@pytest.mark.asyncio
async def test_discover_returns_discovery_result():
    client = _StubHTTPClient(responses={
        "https://example.com": (_MINIMAL_HTML, "ok", 200),
        "https://example.com/sitemap.xml": (b"", "not_found", 404),
    })
    result = await discover_org(
        seed_url="https://example.com",
        seed_etld1="example.com",
        client=client,
        robots_text="",
        ein="12-3456789",
    )
    assert isinstance(result, DiscoveryResult)
    assert result.homepage_ok is True
    assert result.robots_disallowed_all is False


@pytest.mark.asyncio
async def test_discover_homepage_unreachable():
    client = _StubHTTPClient(responses={
        "https://down.org": (b"", "network_error", 0),
        "https://down.org/sitemap.xml": (b"", "network_error", 0),
    })
    result = await discover_org(
        seed_url="https://down.org",
        seed_etld1="down.org",
        client=client,
        robots_text="",
        ein="99-0000000",
    )
    assert result.homepage_ok is False
    assert result.candidates == []


@pytest.mark.asyncio
async def test_discover_robots_disallows_all():
    client = _StubHTTPClient(responses={
        "https://blocked.org/sitemap.xml": (b"", "not_found", 404),
    })
    result = await discover_org(
        seed_url="https://blocked.org",
        seed_etld1="blocked.org",
        client=client,
        robots_text="User-agent: *\nDisallow: /\n",
        ein="88-0000000",
    )
    assert result.robots_disallowed_all is True
    assert result.homepage_ok is False


@pytest.mark.asyncio
async def test_discover_uses_fetcher_callback():
    """When fetcher is provided, discover_org uses it for homepage/subpage/sitemap."""
    fetch_log: list[tuple[str, str]] = []

    async def mock_fetcher(url: str, kind: str) -> tuple[bytes, str]:
        fetch_log.append((url, kind))
        if url.endswith("/sitemap.xml"):
            return b"", "not_found"
        if kind == "homepage":
            return _MINIMAL_HTML, "ok"
        return b"", "not_found"

    client = _StubHTTPClient()
    result = await discover_org(
        seed_url="https://example.com",
        seed_etld1="example.com",
        client=client,
        robots_text="",
        ein="12-3456789",
        fetcher=mock_fetcher,
    )
    assert result.homepage_ok is True
    kinds = [kind for _, kind in fetch_log]
    assert "homepage" in kinds
    assert "sitemap" in kinds
    assert len(client.calls) == 0


@pytest.mark.asyncio
async def test_discover_fetcher_receives_retries():
    """Verify the fetcher callback can implement retries transparently."""
    attempt_count = [0]

    async def retrying_fetcher(url: str, kind: str) -> tuple[bytes, str]:
        attempt_count[0] += 1
        if kind == "sitemap":
            return b"", "not_found"
        if kind == "homepage" and attempt_count[0] == 1:
            return b"", "network_error"
        return _MINIMAL_HTML, "ok"

    client = _StubHTTPClient()
    result = await discover_org(
        seed_url="https://example.com",
        seed_etld1="example.com",
        client=client,
        robots_text="",
        ein="12-3456789",
        fetcher=retrying_fetcher,
    )
    assert result.homepage_ok is True


@pytest.mark.asyncio
async def test_discover_extracts_pdf_candidates():
    html = b"""<html><body>
    <a href="https://example.com/2024-annual-report.pdf">2024 Annual Report</a>
    <a href="https://example.com/impact-report.pdf">Impact Report</a>
    </body></html>"""
    client = _StubHTTPClient(responses={
        "https://example.com": (html, "ok", 200),
        "https://example.com/sitemap.xml": (b"", "not_found", 404),
    })
    result = await discover_org(
        seed_url="https://example.com",
        seed_etld1="example.com",
        client=client,
        robots_text="",
        ein="12-3456789",
    )
    pdf_urls = [c.url for c in result.candidates if c.url.endswith(".pdf")]
    assert len(pdf_urls) >= 1


# ---------- AC26: sync/async parity ----------

def _make_parity_fetcher_responses():
    return {
        "https://example.com": (_PARITY_HTML, "ok"),
        "https://example.com/sitemap.xml": (_PARITY_SITEMAP, "ok"),
        "https://example.com/about": (b"<html><body>About page</body></html>", "ok"),
        "https://example.com/reports": (_PARITY_SUBPAGE_HTML, "ok"),
        "https://example.com/year-in-review": (
            b"<html><body>Year in review page</body></html>", "ok"
        ),
    }


def _sync_fetcher(url: str, kind: str) -> tuple[bytes, str]:
    responses = _make_parity_fetcher_responses()
    return responses.get(url, (b"", "not_found"))


async def _async_fetcher(url: str, kind: str) -> tuple[bytes, str]:
    responses = _make_parity_fetcher_responses()
    return responses.get(url, (b"", "not_found"))


@pytest.mark.asyncio
async def test_ac26_async_matches_sync_candidates():
    """AC26: same canned HTML produces identical candidate set in sync and async."""
    sync_candidates = per_org_candidates(
        seed_url="https://example.com",
        seed_etld1="example.com",
        fetcher=_sync_fetcher,
        robots_text="",
        ein="12-3456789",
    )

    client = _StubHTTPClient()
    async_result = await discover_org(
        seed_url="https://example.com",
        seed_etld1="example.com",
        client=client,
        robots_text="",
        ein="12-3456789",
        fetcher=_async_fetcher,
    )
    async_candidates = async_result.candidates

    sync_urls = sorted(c.url for c in sync_candidates)
    async_urls = sorted(c.url for c in async_result.candidates)
    assert sync_urls == async_urls, (
        f"URL mismatch:\n  sync:  {sync_urls}\n  async: {async_urls}"
    )

    sync_by_url = {c.url: c for c in sync_candidates}
    async_by_url = {c.url: c for c in async_candidates}
    for url in sync_by_url:
        sc = sync_by_url[url]
        ac = async_by_url[url]
        assert sc.hosting_platform == ac.hosting_platform, (
            f"{url}: hosting_platform {sc.hosting_platform!r} != {ac.hosting_platform!r}"
        )
        assert sc.attribution_confidence == ac.attribution_confidence, (
            f"{url}: attribution_confidence {sc.attribution_confidence!r} != {ac.attribution_confidence!r}"
        )
        assert sc.discovered_via == ac.discovered_via, (
            f"{url}: discovered_via {sc.discovered_via!r} != {ac.discovered_via!r}"
        )

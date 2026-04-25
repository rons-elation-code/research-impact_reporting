"""Tests for wayback_fallback.py (AC4-AC7, AC11, AC15.2-AC15.5, AC20-AC25)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

from lavandula.reports.wayback_fallback import (
    WaybackOutcome,
    WaybackResult,
    _dedupe_and_cap,
    _parse_cdx_response,
    _row_to_candidate,
    discover_via_wayback,
)
from lavandula.reports.wayback_validation import build_cdx_url


# ---------- Fixtures ----------

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


@dataclass
class _FakeClient:
    responses: dict[str, _FakeResult] = field(default_factory=dict)
    default_response: _FakeResult | None = None
    calls: list[tuple[str, str, float | None]] = field(default_factory=list)

    async def get(self, url, *, kind="homepage", seed_etld1=None,
                  extra_headers=None, timeout_override=None):
        self.calls.append((url, kind, timeout_override))
        if url in self.responses:
            return self.responses[url]
        for prefix, resp in self.responses.items():
            if url.startswith(prefix):
                return resp
        return self.default_response or _FakeResult(status="network_error", body=None)


@dataclass
class _FakeStats:
    wayback_attempts: int = 0
    wayback_recoveries: int = 0
    wayback_empty: int = 0
    wayback_errors: int = 0


_CDX_HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]


def _make_cdx_json(*rows) -> bytes:
    data = [_CDX_HEADER] + list(rows)
    return json.dumps(data).encode()


# ---------- _parse_cdx_response ----------

class TestParseCdxResponse:
    def test_empty_list(self):
        outcome, validated, raw = _parse_cdx_response(b"[]")
        assert outcome == WaybackOutcome.EMPTY
        assert validated == []
        assert raw == 0

    def test_header_only(self):
        body = json.dumps([_CDX_HEADER]).encode()
        outcome, validated, raw = _parse_cdx_response(body)
        assert outcome == WaybackOutcome.EMPTY
        assert raw == 0

    def test_valid_rows(self):
        body = _make_cdx_json(
            ["org,sloan)/r1.pdf", "20260406121250", "https://sloan.org/r1.pdf",
             "application/pdf", "200", "DEADBEEF", "1000"],
            ["org,sloan)/r2.pdf", "20260305101010", "https://sloan.org/r2.pdf",
             "application/pdf", "200", "CAFEBABE", "2000"],
        )
        outcome, validated, raw = _parse_cdx_response(body)
        assert outcome == WaybackOutcome.RECOVERED
        assert len(validated) == 2
        assert raw == 2

    def test_non_json_body(self):
        outcome, validated, raw = _parse_cdx_response(b"<html>rate limited</html>")
        assert outcome == WaybackOutcome.ERROR

    def test_all_rows_malformed(self):
        body = json.dumps([_CDX_HEADER, ["short"]]).encode()
        outcome, validated, raw = _parse_cdx_response(body)
        assert outcome == WaybackOutcome.EMPTY
        assert raw == 1

    def test_unicode_decode_error(self):
        outcome, validated, raw = _parse_cdx_response(b"\xff\xfe")
        assert outcome == WaybackOutcome.ERROR

    def test_mixed_valid_and_invalid(self):
        body = _make_cdx_json(
            ["uk", "20260406121250", "https://x.org/good.pdf", "p", "200", "d", "1"],
            ["bad"],
            ["uk2", "badts", "https://x.org/bad.pdf", "p", "200", "d", "1"],
        )
        outcome, validated, raw = _parse_cdx_response(body)
        assert outcome == WaybackOutcome.RECOVERED
        assert len(validated) == 1
        assert raw == 3


# ---------- _dedupe_and_cap ----------

class TestDedupeAndCap:
    def test_dedup_picks_max_timestamp(self):
        rows = [
            {"urlkey": "same", "timestamp": "20200101000000",
             "original": "https://x.org/f.pdf", "capture_host": "x.org", "digest": None},
            {"urlkey": "same", "timestamp": "20260101000000",
             "original": "https://x.org/f.pdf", "capture_host": "x.org", "digest": None},
        ]
        deduped, hosts = _dedupe_and_cap(rows, "x.org", max_pdfs=30, max_subdomains=3)
        assert len(deduped) == 1
        assert deduped[0]["timestamp"] == "20260101000000"

    def test_sorts_by_timestamp_desc(self):
        rows = [
            {"urlkey": "a", "timestamp": "20200101000000",
             "original": "https://x.org/a.pdf", "capture_host": "x.org", "digest": None},
            {"urlkey": "b", "timestamp": "20260101000000",
             "original": "https://x.org/b.pdf", "capture_host": "x.org", "digest": None},
        ]
        deduped, hosts = _dedupe_and_cap(rows, "x.org", max_pdfs=30, max_subdomains=3)
        assert deduped[0]["urlkey"] == "b"

    def test_cross_etld1_dropped(self):
        rows = [
            {"urlkey": "good", "timestamp": "20260101000000",
             "original": "https://sloan.org/f.pdf", "capture_host": "sloan.org", "digest": None},
            {"urlkey": "bad", "timestamp": "20260101000000",
             "original": "https://evil.com/f.pdf", "capture_host": "evil.com", "digest": None},
        ]
        deduped, hosts = _dedupe_and_cap(rows, "sloan.org", max_pdfs=30, max_subdomains=3)
        assert len(deduped) == 1
        assert deduped[0]["capture_host"] == "sloan.org"

    def test_subdomain_cap(self):
        rows = [
            {"urlkey": f"h{i}", "timestamp": f"2026010100000{i}",
             "original": f"https://sub{i}.x.org/f.pdf",
             "capture_host": f"sub{i}.x.org", "digest": None}
            for i in range(5)
        ]
        deduped, hosts = _dedupe_and_cap(rows, "x.org", max_pdfs=30, max_subdomains=3)
        distinct = {r["capture_host"] for r in deduped}
        assert len(distinct) <= 3

    def test_apex_preferred(self):
        rows = [
            {"urlkey": "apex", "timestamp": "20200101000000",
             "original": "https://sloan.org/f.pdf", "capture_host": "sloan.org", "digest": None},
            {"urlkey": "s1", "timestamp": "20260101000001",
             "original": "https://a.sloan.org/f.pdf", "capture_host": "a.sloan.org", "digest": None},
            {"urlkey": "s2", "timestamp": "20260101000002",
             "original": "https://b.sloan.org/f.pdf", "capture_host": "b.sloan.org", "digest": None},
            {"urlkey": "s3", "timestamp": "20260101000003",
             "original": "https://c.sloan.org/f.pdf", "capture_host": "c.sloan.org", "digest": None},
        ]
        deduped, hosts = _dedupe_and_cap(rows, "sloan.org", max_pdfs=30, max_subdomains=3)
        assert "sloan.org" in hosts

    def test_cap_at_max_pdfs(self):
        rows = [
            {"urlkey": f"k{i}", "timestamp": f"2026010100{i:04d}",
             "original": f"https://x.org/f{i}.pdf", "capture_host": "x.org", "digest": None}
            for i in range(50)
        ]
        deduped, hosts = _dedupe_and_cap(rows, "x.org", max_pdfs=5, max_subdomains=3)
        assert len(deduped) == 5


# ---------- _row_to_candidate ----------

class TestRowToCandidate:
    def test_fields_populated(self):
        row = {
            "urlkey": "uk",
            "timestamp": "20260406121250",
            "original": "https://sloan.org/report.pdf",
            "capture_host": "sloan.org",
            "digest": "DEADBEEF",
        }
        c = _row_to_candidate(row, "https://sloan.org")
        assert c.discovered_via == "wayback"
        assert c.hosting_platform == "wayback"
        assert c.attribution_confidence == "wayback_archive"
        assert c.original_source_url == "https://sloan.org/report.pdf"
        assert c.wayback_digest == "DEADBEEF"
        assert "20260406121250id_/" in c.url
        assert c.referring_page_url == "https://sloan.org"


# ---------- discover_via_wayback (full flow) ----------

class TestDiscoverViaWayback:
    @pytest.mark.asyncio
    async def test_invalid_domain(self):
        stats = _FakeStats()
        result = await discover_via_wayback(
            seed_url="https://evil.org&matchType=exact/",
            seed_etld1="evil.org&matchType=exact",
            client=_FakeClient(),
            ein="123",
            stats=stats,
        )
        assert result.outcome == WaybackOutcome.INVALID_DOMAIN
        assert stats.wayback_attempts == 0
        assert not result.cdx_query_fired

    @pytest.mark.asyncio
    async def test_network_error(self):
        client = _FakeClient(default_response=_FakeResult(
            status="network_error", body=None, http_status=None,
        ))
        stats = _FakeStats()
        result = await discover_via_wayback(
            seed_url="https://sloan.org",
            seed_etld1="sloan.org",
            client=client,
            ein="123",
            stats=stats,
        )
        assert result.outcome == WaybackOutcome.ERROR
        assert stats.wayback_attempts == 1
        assert result.cdx_query_fired

    @pytest.mark.asyncio
    async def test_empty_cdx(self):
        body = json.dumps([_CDX_HEADER]).encode()
        client = _FakeClient(default_response=_FakeResult(body=body))
        stats = _FakeStats()
        result = await discover_via_wayback(
            seed_url="https://sloan.org",
            seed_etld1="sloan.org",
            client=client,
            ein="123",
            stats=stats,
        )
        assert result.outcome == WaybackOutcome.EMPTY
        assert result.cdx_http_status == 200

    @pytest.mark.asyncio
    async def test_recovered(self):
        body = _make_cdx_json(
            ["org,sloan)/r1.pdf", "20260406121250", "https://sloan.org/r1.pdf",
             "application/pdf", "200", "D1", "1000"],
            ["org,sloan)/r2.pdf", "20260305101010", "https://sloan.org/r2.pdf",
             "application/pdf", "200", "D2", "2000"],
        )
        client = _FakeClient(default_response=_FakeResult(body=body))
        stats = _FakeStats()
        result = await discover_via_wayback(
            seed_url="https://sloan.org",
            seed_etld1="sloan.org",
            client=client,
            ein="123",
            stats=stats,
        )
        assert result.outcome == WaybackOutcome.RECOVERED
        assert len(result.candidates) == 2
        assert result.candidates[0].discovered_via == "wayback"
        assert "sloan.org" in result.capture_hosts
        assert stats.wayback_attempts == 1

    @pytest.mark.asyncio
    async def test_timeout_override_15s(self):
        client = _FakeClient(default_response=_FakeResult(
            status="network_error", body=None,
        ))
        stats = _FakeStats()
        await discover_via_wayback(
            seed_url="https://sloan.org",
            seed_etld1="sloan.org",
            client=client,
            ein="123",
            stats=stats,
        )
        assert len(client.calls) == 1
        _, kind, timeout = client.calls[0]
        assert kind == "wayback-cdx"
        assert timeout == 15.0

    @pytest.mark.asyncio
    async def test_html_body_treated_as_error(self):
        client = _FakeClient(default_response=_FakeResult(
            body=b"<html>maintenance page</html>",
        ))
        stats = _FakeStats()
        result = await discover_via_wayback(
            seed_url="https://sloan.org",
            seed_etld1="sloan.org",
            client=client,
            ein="123",
            stats=stats,
        )
        assert result.outcome == WaybackOutcome.ERROR

    @pytest.mark.asyncio
    async def test_all_rows_cross_domain_produces_empty(self):
        body = _make_cdx_json(
            ["com,evil)/f.pdf", "20260406121250", "https://evil.com/f.pdf",
             "application/pdf", "200", "D1", "1000"],
        )
        client = _FakeClient(default_response=_FakeResult(body=body))
        stats = _FakeStats()
        result = await discover_via_wayback(
            seed_url="https://sloan.org",
            seed_etld1="sloan.org",
            client=client,
            ein="123",
            stats=stats,
        )
        assert result.outcome == WaybackOutcome.EMPTY

    @pytest.mark.asyncio
    async def test_size_capped_treated_as_error(self):
        client = _FakeClient(default_response=_FakeResult(
            status="size_capped", body=None, http_status=200,
        ))
        stats = _FakeStats()
        result = await discover_via_wayback(
            seed_url="https://sloan.org",
            seed_etld1="sloan.org",
            client=client,
            ein="123",
            stats=stats,
        )
        assert result.outcome == WaybackOutcome.ERROR

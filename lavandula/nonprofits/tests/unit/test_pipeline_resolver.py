"""Unit tests for pipeline_resolver.py (Spec 0018)."""
from __future__ import annotations

import json
import queue
import signal
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from lavandula.nonprofits.pipeline_resolver import (
    ConsumerStats,
    PipelineQueue,
    ProducerStats,
    ShutdownFlag,
    consumer,
    install_sigint_handler,
    producer,
)


# ── Queue tests ───────────────────────────────────────────────────────────────


class TestPipelineQueue:
    def test_queue_put_get(self):
        pq = PipelineQueue(maxsize=4)
        pq.put({"ein": "123"})
        result = pq.get(timeout=1.0)
        assert result == {"ein": "123"}

    def test_sentinel_terminates_consumer(self):
        pq = PipelineQueue(maxsize=4)
        pq.put({"ein": "123"})
        pq.done()
        assert pq.get(timeout=1.0) == {"ein": "123"}
        assert pq.get(timeout=1.0) is None

    def test_backpressure(self):
        pq = PipelineQueue(maxsize=2)
        pq.put({"a": 1})
        pq.put({"b": 2})
        with pytest.raises(queue.Full):
            pq.put({"c": 3}, timeout=0.1)

    def test_qsize_tracks_depth(self):
        pq = PipelineQueue(maxsize=8)
        for i in range(5):
            pq.put({"i": i})
        assert pq.qsize == 5


class TestShutdownFlag:
    def test_sigint_sets_flag(self):
        """AC23 partial: SIGINT sets the shutdown flag."""
        flag = ShutdownFlag()
        install_sigint_handler(flag)
        assert not flag.is_set()
        signal.raise_signal(signal.SIGINT)
        assert flag.is_set()
        signal.signal(signal.SIGINT, signal.SIG_DFL)


# ── Producer tests ────────────────────────────────────────────────────────────


def _make_mock_engine():
    engine = MagicMock()
    conn = MagicMock()
    engine.begin.return_value.__enter__ = MagicMock(return_value=conn)
    engine.begin.return_value.__exit__ = MagicMock(return_value=False)
    return engine


class TestProducer:
    def test_producer_enqueues_packets(self):
        """Mock Brave + fetch → packets appear in queue."""
        pq = PipelineQueue(maxsize=32)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()

        orgs = [
            {"ein": "111111111", "name": "Org A", "city": "Dallas", "state": "TX"},
            {"ein": "222222222", "name": "Org B", "city": "Austin", "state": "TX"},
        ]

        from lavandula.nonprofits.brave_search import BraveRateLimiter, BraveSearchResult

        rl = BraveRateLimiter(100.0)

        def mock_search_and_filter(name, city, state, *, api_key, rate_limiter, max_results=3):
            return [BraveSearchResult(title=f"{name} Site", url=f"https://{name.lower().replace(' ', '')}.org", snippet="...")]

        def mock_fetch(url):
            return {
                "url": url, "final_url": url, "live": True,
                "title": "", "snippet": "", "excerpt": "Welcome",
                "status_code": 200,
            }

        with patch("lavandula.nonprofits.pipeline_resolver.search_and_filter", side_effect=mock_search_and_filter):
            with patch("lavandula.nonprofits.pipeline_resolver._fetch_candidate", side_effect=mock_fetch):
                stats = producer(
                    orgs, pq=pq, engine=engine, api_key="key",
                    rate_limiter=rl, shutdown=shutdown,
                )

        assert stats.searched == 2
        assert stats.enqueued == 2

        packets = []
        while True:
            p = pq.get(timeout=1.0)
            if p is None:
                break
            packets.append(p)
        assert len(packets) == 2

    def test_producer_skips_no_results(self):
        pq = PipelineQueue(maxsize=32)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()
        orgs = [{"ein": "111111111", "name": "Ghost Org", "city": "Nowhere", "state": "TX"}]

        from lavandula.nonprofits.brave_search import BraveRateLimiter

        rl = BraveRateLimiter(100.0)

        with patch("lavandula.nonprofits.pipeline_resolver.search_and_filter", return_value=[]):
            stats = producer(
                orgs, pq=pq, engine=engine, api_key="key",
                rate_limiter=rl, shutdown=shutdown,
            )

        assert stats.skipped_no_results == 1
        assert stats.enqueued == 0

    def test_producer_skips_no_live(self):
        pq = PipelineQueue(maxsize=32)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()
        orgs = [{"ein": "111111111", "name": "Dead Org", "city": "X", "state": "TX"}]

        from lavandula.nonprofits.brave_search import BraveRateLimiter, BraveSearchResult

        rl = BraveRateLimiter(100.0)

        def mock_search(*args, **kwargs):
            return [BraveSearchResult(title="Dead", url="https://dead.org", snippet="...")]

        def mock_fetch(url):
            return {"url": url, "final_url": url, "live": False, "title": "", "snippet": "", "excerpt": "", "status_code": 404}

        with patch("lavandula.nonprofits.pipeline_resolver.search_and_filter", side_effect=mock_search):
            with patch("lavandula.nonprofits.pipeline_resolver._fetch_candidate", side_effect=mock_fetch):
                stats = producer(
                    orgs, pq=pq, engine=engine, api_key="key",
                    rate_limiter=rl, shutdown=shutdown,
                )

        assert stats.skipped_no_live == 1

    def test_producer_brave_error_reason(self):
        pq = PipelineQueue(maxsize=32)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()
        orgs = [{"ein": "111111111", "name": "Err Org", "city": "X", "state": "TX"}]

        from lavandula.nonprofits.brave_search import BraveRateLimiter, BraveSearchError

        rl = BraveRateLimiter(100.0)

        with patch("lavandula.nonprofits.pipeline_resolver.search_and_filter", side_effect=BraveSearchError("Brave API returned 500")):
            stats = producer(
                orgs, pq=pq, engine=engine, api_key="key",
                rate_limiter=rl, shutdown=shutdown,
            )

        assert stats.brave_errors == 1

    def test_producer_shutdown_stops_early(self):
        pq = PipelineQueue(maxsize=32)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()
        orgs = [{"ein": f"{i:09d}", "name": f"Org {i}", "city": "X", "state": "TX"} for i in range(10)]

        from lavandula.nonprofits.brave_search import BraveRateLimiter, BraveSearchResult

        rl = BraveRateLimiter(100.0)
        call_count = 0

        def mock_search(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                shutdown.set()
            return [BraveSearchResult(title="Site", url="https://site.org", snippet="...")]

        def mock_fetch(url):
            return {"url": url, "final_url": url, "live": True, "title": "", "snippet": "", "excerpt": "ok", "status_code": 200}

        with patch("lavandula.nonprofits.pipeline_resolver.search_and_filter", side_effect=mock_search):
            with patch("lavandula.nonprofits.pipeline_resolver._fetch_candidate", side_effect=mock_fetch):
                stats = producer(
                    orgs, pq=pq, engine=engine, api_key="key",
                    rate_limiter=rl, shutdown=shutdown,
                )

        assert stats.searched <= 4


# ── Consumer tests ────────────────────────────────────────────────────────────


class TestConsumer:
    def test_consumer_resolves_high_confidence(self):
        pq = PipelineQueue(maxsize=4)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()

        pq.put({
            "ein": "111111111", "name": "Test", "city": "X", "state": "TX",
            "candidates": [{"url": "https://test.org", "final_url": "https://test.org", "excerpt": "ok"}],
        })
        pq.done()

        mock_gemma = MagicMock()
        mock_gemma.disambiguate.return_value = {
            "url": "https://test.org", "confidence": 0.95, "reasoning": "match"
        }

        stats = consumer(pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown)
        assert stats.resolved == 1

    def test_consumer_unresolved_low_confidence(self):
        pq = PipelineQueue(maxsize=4)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()

        pq.put({
            "ein": "111111111", "name": "Test", "city": "X", "state": "TX",
            "candidates": [{"url": "https://test.org", "final_url": "https://test.org", "excerpt": "ok"}],
        })
        pq.done()

        mock_gemma = MagicMock()
        mock_gemma.disambiguate.return_value = {
            "url": "https://test.org", "confidence": 0.3, "reasoning": "unsure"
        }

        stats = consumer(pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown)
        assert stats.unresolved == 1

    def test_consumer_retry_on_connection_error(self):
        """AC11: retry on ConnectionError, then succeed."""
        pq = PipelineQueue(maxsize=4)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()

        pq.put({
            "ein": "111111111", "name": "Test", "city": "X", "state": "TX",
            "candidates": [{"url": "https://test.org", "final_url": "https://test.org", "excerpt": "ok"}],
        })
        pq.done()

        import requests as http_requests
        mock_gemma = MagicMock()
        mock_gemma.disambiguate.side_effect = [
            http_requests.ConnectionError("tunnel down"),
            http_requests.ConnectionError("still down"),
            {"url": "https://test.org", "confidence": 0.9, "reasoning": "ok"},
        ]

        with patch("lavandula.nonprofits.pipeline_resolver.time.sleep"):
            stats = consumer(pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown)

        assert stats.resolved == 1

    def test_consumer_inference_unavailable(self):
        """AC11: after all retries exhausted, marks inference_unavailable."""
        pq = PipelineQueue(maxsize=4)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()

        pq.put({
            "ein": "111111111", "name": "Test", "city": "X", "state": "TX",
            "candidates": [{"url": "https://test.org", "final_url": "https://test.org", "excerpt": "ok"}],
        })
        pq.done()

        import requests as http_requests
        mock_gemma = MagicMock()
        mock_gemma.disambiguate.side_effect = http_requests.ConnectionError("down")

        with patch("lavandula.nonprofits.pipeline_resolver.time.sleep"):
            stats = consumer(pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown)

        assert stats.unresolved == 1

    def test_consumer_parse_error(self):
        pq = PipelineQueue(maxsize=4)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()

        pq.put({
            "ein": "111111111", "name": "Test", "city": "X", "state": "TX",
            "candidates": [{"url": "https://test.org", "final_url": "https://test.org", "excerpt": "ok"}],
        })
        pq.done()

        from lavandula.nonprofits.gemma_client import GemmaParseError
        mock_gemma = MagicMock()
        mock_gemma.disambiguate.side_effect = GemmaParseError("bad json")

        stats = consumer(pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown)
        assert stats.unresolved == 1

    def test_consumer_db_write_failure(self):
        """AC24: DB write failure doesn't crash the pipeline."""
        pq = PipelineQueue(maxsize=4)
        shutdown = ShutdownFlag()

        engine = MagicMock()
        engine.begin.return_value.__enter__ = MagicMock(side_effect=RuntimeError("db error"))
        engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        pq.put({
            "ein": "111111111", "name": "Test", "city": "X", "state": "TX",
            "candidates": [{"url": "https://test.org", "final_url": "https://test.org", "excerpt": "ok"}],
        })
        pq.put({
            "ein": "222222222", "name": "Test2", "city": "Y", "state": "TX",
            "candidates": [{"url": "https://test2.org", "final_url": "https://test2.org", "excerpt": "ok"}],
        })
        pq.done()

        mock_gemma = MagicMock()
        mock_gemma.disambiguate.return_value = {
            "url": "https://test.org", "confidence": 0.9, "reasoning": "match"
        }

        stats = consumer(pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown)
        assert stats.errors == 2

    def test_consumer_stops_on_sentinel(self):
        pq = PipelineQueue(maxsize=4)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()
        pq.done()

        mock_gemma = MagicMock()
        stats = consumer(pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown)
        assert stats.resolved == 0
        mock_gemma.disambiguate.assert_not_called()


class TestQueueDepth:
    def test_queue_depth_tracked(self):
        """AC7: queue reaches depth > 0 during a run with fast producer."""
        pq = PipelineQueue(maxsize=32)
        engine = _make_mock_engine()
        shutdown = ShutdownFlag()

        from lavandula.nonprofits.brave_search import BraveRateLimiter, BraveSearchResult

        rl = BraveRateLimiter(1000.0)
        orgs = [{"ein": f"{i:09d}", "name": f"Org {i}", "city": "X", "state": "TX"} for i in range(20)]

        def mock_search(*args, **kwargs):
            return [BraveSearchResult(title="Site", url="https://site.org", snippet="...")]

        def mock_fetch(url):
            return {"url": url, "final_url": url, "live": True, "title": "", "snippet": "", "excerpt": "ok", "status_code": 200}

        mock_gemma = MagicMock()

        def slow_disambiguate(org, candidates):
            time.sleep(0.05)
            return {"url": "https://site.org", "confidence": 0.9, "reasoning": "ok"}

        mock_gemma.disambiguate.side_effect = slow_disambiguate

        with patch("lavandula.nonprofits.pipeline_resolver.search_and_filter", side_effect=mock_search):
            with patch("lavandula.nonprofits.pipeline_resolver._fetch_candidate", side_effect=mock_fetch):
                producer_thread = threading.Thread(
                    target=producer,
                    kwargs={
                        "orgs": orgs, "pq": pq, "engine": engine,
                        "api_key": "key", "rate_limiter": rl, "shutdown": shutdown,
                    },
                    daemon=True,
                )
                producer_thread.start()
                stats = consumer(pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown)
                producer_thread.join(timeout=5)

        assert stats.max_queue_depth > 0

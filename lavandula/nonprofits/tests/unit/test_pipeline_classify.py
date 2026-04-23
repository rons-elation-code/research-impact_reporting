"""Unit tests for pipeline_classify.py (Spec 0018)."""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
import requests as http_requests

from lavandula.nonprofits.gemma_client import GemmaParseError
from lavandula.nonprofits.pipeline_classify import (
    ClassifyConsumerStats,
    ClassifyProducerStats,
    classify_consumer,
    classify_producer,
)
from lavandula.nonprofits.pipeline_resolver import PipelineQueue, ShutdownFlag


def _make_mock_engine_with_rows(rows_by_page: list[list[tuple]]):
    """Create a mock engine that returns paginated results."""
    engine = MagicMock()
    page_idx = [0]

    def mock_connect():
        ctx = MagicMock()
        conn = MagicMock()

        def mock_execute(sql, params=None):
            result = MagicMock()
            idx = page_idx[0]
            if idx < len(rows_by_page):
                result.fetchall.return_value = rows_by_page[idx]
                page_idx[0] += 1
            else:
                result.fetchall.return_value = []
            return result

        conn.execute = mock_execute
        ctx.__enter__ = MagicMock(return_value=conn)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    engine.connect = mock_connect

    begin_conn = MagicMock()
    engine.begin.return_value.__enter__ = MagicMock(return_value=begin_conn)
    engine.begin.return_value.__exit__ = MagicMock(return_value=False)

    return engine


class TestClassifyProducer:
    def test_classify_producer_paginates(self):
        """Keyset pagination issues multiple queries."""
        page1 = [(f"sha_{i:03d}", f"Page text {i}") for i in range(20)]
        page2 = [(f"sha_{i:03d}", f"Page text {i}") for i in range(20, 40)]
        page3 = [(f"sha_{i:03d}", f"Page text {i}") for i in range(40, 50)]

        engine = _make_mock_engine_with_rows([page1, page2, page3, []])

        pq = PipelineQueue(maxsize=100)
        shutdown = ShutdownFlag()

        with patch("lavandula.nonprofits.pipeline_classify._PAGE_SIZE", 20):
            stats = classify_producer(
                engine=engine, pq=pq, shutdown=shutdown,
            )

        assert stats.scanned == 50
        assert stats.enqueued == 50

    def test_classify_producer_skips_null_text(self):
        """Reports with NULL first_page_text are skipped."""
        rows = [
            ("sha_001", "Some text"),
            ("sha_002", None),
            ("sha_003", ""),
            ("sha_004", "More text"),
        ]
        engine = _make_mock_engine_with_rows([rows, []])

        pq = PipelineQueue(maxsize=100)
        shutdown = ShutdownFlag()

        stats = classify_producer(
            engine=engine, pq=pq, shutdown=shutdown,
        )

        assert stats.enqueued == 2
        assert stats.skipped_no_text == 2


class TestClassifyConsumer:
    def _make_simple_engine(self):
        engine = MagicMock()
        conn = MagicMock()
        engine.begin.return_value.__enter__ = MagicMock(return_value=conn)
        engine.begin.return_value.__exit__ = MagicMock(return_value=False)
        return engine

    def test_classify_consumer_writes_result(self):
        """Gemma returns annual/0.95 → DB UPDATE with correct values."""
        pq = PipelineQueue(maxsize=4)
        engine = self._make_simple_engine()
        shutdown = ShutdownFlag()

        pq.put({"content_sha256": "abc123", "first_page_text": "Annual report 2025..."})
        pq.done()

        mock_gemma = MagicMock()
        mock_gemma.classify.return_value = {
            "classification": "annual",
            "confidence": 0.95,
            "reasoning": "Annual report",
        }

        stats = classify_consumer(
            pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown,
        )

        assert stats.classified == 1

    def test_classify_consumer_retry_on_connection_error(self):
        pq = PipelineQueue(maxsize=4)
        engine = self._make_simple_engine()
        shutdown = ShutdownFlag()

        pq.put({"content_sha256": "abc", "first_page_text": "text"})
        pq.done()

        mock_gemma = MagicMock()
        mock_gemma.classify.side_effect = [
            http_requests.ConnectionError("down"),
            http_requests.ConnectionError("still down"),
            {"classification": "impact", "confidence": 0.85, "reasoning": "ok"},
        ]

        with patch("lavandula.nonprofits.pipeline_classify.time.sleep"):
            stats = classify_consumer(
                pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown,
            )

        assert stats.classified == 1

    def test_classify_consumer_parse_error(self):
        pq = PipelineQueue(maxsize=4)
        engine = self._make_simple_engine()
        shutdown = ShutdownFlag()

        pq.put({"content_sha256": "abc", "first_page_text": "text"})
        pq.done()

        mock_gemma = MagicMock()
        mock_gemma.classify.side_effect = GemmaParseError("bad response")

        stats = classify_consumer(
            pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown,
        )

        assert stats.errors == 1

    def test_classify_consumer_db_failure(self):
        pq = PipelineQueue(maxsize=4)
        shutdown = ShutdownFlag()

        engine = MagicMock()
        engine.begin.return_value.__enter__ = MagicMock(side_effect=RuntimeError("db fail"))
        engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        pq.put({"content_sha256": "abc", "first_page_text": "text"})
        pq.done()

        mock_gemma = MagicMock()
        mock_gemma.classify.return_value = {
            "classification": "annual", "confidence": 0.9, "reasoning": "ok"
        }

        stats = classify_consumer(
            pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown,
        )

        assert stats.errors == 1

    def test_classify_consumer_stops_on_sentinel(self):
        pq = PipelineQueue(maxsize=4)
        engine = self._make_simple_engine()
        shutdown = ShutdownFlag()
        pq.done()

        mock_gemma = MagicMock()
        stats = classify_consumer(
            pq=pq, gemma=mock_gemma, engine=engine, shutdown=shutdown,
        )

        assert stats.classified == 0
        mock_gemma.classify.assert_not_called()

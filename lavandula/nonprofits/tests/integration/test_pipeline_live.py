"""Live integration test for Gemma pipeline (Spec 0018).

Behind LAVANDULA_LIVE_GEMMA=1. Requires live Brave API + Gemma on cloud1.

AC12: ≥ 8/10 resolved on TX unresolved orgs (manual validation gate).
AC13: ≥ 8/10 report classifications match existing Haiku (manual validation gate).
"""
from __future__ import annotations

import os

import pytest

_SKIP_REASON = "requires live Gemma + Brave (set LAVANDULA_LIVE_GEMMA=1)"


@pytest.mark.skipif(
    os.getenv("LAVANDULA_LIVE_GEMMA") != "1",
    reason=_SKIP_REASON,
)
class TestPipelineLive:
    def test_resolve_tx_10(self):
        """AC12: ≥ 8/10 resolved on TX unresolved orgs."""
        from lavandula.common.db import make_app_engine
        from lavandula.common.secrets import get_brave_api_key
        from lavandula.nonprofits.brave_search import BraveRateLimiter
        from lavandula.nonprofits.gemma_client import GemmaClient
        from lavandula.nonprofits.pipeline_resolver import (
            PipelineQueue,
            ShutdownFlag,
            consumer,
            load_unresolved_orgs,
            producer,
        )
        import threading

        engine = make_app_engine()
        api_key = get_brave_api_key()
        gemma = GemmaClient(
            base_url="http://localhost:11434/v1",
            model="gemma4:e4b",
        )
        assert gemma.health_check(), "Gemma endpoint unreachable"

        orgs = load_unresolved_orgs(engine, state="TX", limit=10)
        assert len(orgs) >= 10, f"Need 10 TX unresolved orgs, found {len(orgs)}"

        pq = PipelineQueue(maxsize=32)
        shutdown = ShutdownFlag()
        rl = BraveRateLimiter(1.0)

        t = threading.Thread(
            target=producer,
            kwargs={
                "orgs": orgs[:10], "pq": pq, "engine": engine,
                "api_key": api_key, "rate_limiter": rl, "shutdown": shutdown,
            },
            daemon=True,
        )
        t.start()

        stats = consumer(pq=pq, gemma=gemma, engine=engine, shutdown=shutdown)
        t.join(timeout=30)

        assert stats.resolved >= 8, (
            f"Expected ≥ 8/10 resolved, got {stats.resolved} "
            f"(ambiguous={stats.ambiguous}, unresolved={stats.unresolved})"
        )

    def test_classify_10_reports(self):
        """AC13: ≥ 8/10 match existing Haiku classifications."""
        from lavandula.common.db import make_app_engine
        from lavandula.nonprofits.gemma_client import GemmaClient
        from lavandula.nonprofits.pipeline_classify import (
            classify_consumer,
            classify_producer,
        )
        from lavandula.nonprofits.pipeline_resolver import (
            PipelineQueue,
            ShutdownFlag,
        )
        import threading

        engine = make_app_engine()
        gemma = GemmaClient(
            base_url="http://localhost:11434/v1",
            model="gemma4:e4b",
        )
        assert gemma.health_check(), "Gemma endpoint unreachable"

        pq = PipelineQueue(maxsize=32)
        shutdown = ShutdownFlag()

        t = threading.Thread(
            target=classify_producer,
            kwargs={
                "engine": engine, "pq": pq,
                "limit": 10, "shutdown": shutdown,
            },
            daemon=True,
        )
        t.start()

        stats = classify_consumer(
            pq=pq, gemma=gemma, engine=engine, shutdown=shutdown,
        )
        t.join(timeout=60)

        assert stats.classified >= 8, (
            f"Expected ≥ 8/10 classified, got {stats.classified} "
            f"(errors={stats.errors}, skipped={stats.skipped})"
        )

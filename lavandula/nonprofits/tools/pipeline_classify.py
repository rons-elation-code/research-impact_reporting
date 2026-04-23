"""CLI entry point for Gemma pipeline report classification (Spec 0018).

Usage:
    python -m lavandula.nonprofits.tools.pipeline_classify [OPTIONS]
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from lavandula.common.db import MIN_SCHEMA_VERSION, assert_schema_at_least, make_app_engine
from lavandula.nonprofits.gemma_client import GemmaClient
from lavandula.nonprofits.pipeline_classify import (
    classify_consumer,
    classify_producer,
)
from lavandula.nonprofits.pipeline_resolver import (
    PipelineQueue,
    ShutdownFlag,
    install_sigint_handler,
)

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipeline_classify",
        description="Classify nonprofit reports via Gemma 4 E4B.",
    )
    p.add_argument("--limit", type=int, default=0, help="Max reports to process (0 = no limit)")
    p.add_argument("--queue-size", type=int, default=32, help="Bounded queue capacity")
    p.add_argument("--gemma-url", default="http://localhost:11434/v1", help="Ollama endpoint")
    p.add_argument("--gemma-model", default="gemma4:e4b", help="Model tag")
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)

    gemma = GemmaClient(base_url=args.gemma_url, model=args.gemma_model)
    if not gemma.health_check():
        print(
            f"ERROR: Gemma endpoint unreachable at {args.gemma_url}",
            file=sys.stderr,
        )
        sys.exit(1)

    engine = make_app_engine()
    assert_schema_at_least(engine, MIN_SCHEMA_VERSION)

    try:
        limit = args.limit if args.limit > 0 else None

        pq = PipelineQueue(maxsize=args.queue_size)
        shutdown = ShutdownFlag()
        install_sigint_handler(shutdown)

        t_start = time.monotonic()

        producer_stats = [None]

        def _run_producer():
            producer_stats[0] = classify_producer(
                engine=engine,
                pq=pq,
                limit=limit,
                shutdown=shutdown,
            )

        producer_thread = threading.Thread(target=_run_producer, daemon=True)
        producer_thread.start()

        consumer_stats = classify_consumer(
            pq=pq, gemma=gemma, engine=engine, shutdown=shutdown,
        )
        producer_thread.join(timeout=10)

        wall_time = time.monotonic() - t_start
        p_stats = producer_stats[0]

        print("\n--- Classification Summary ---")
        print(f"Wall time: {wall_time:.1f}s")
        if p_stats:
            print(f"Scanned: {p_stats.scanned}")
            print(f"Enqueued: {p_stats.enqueued}")
            print(f"Skipped (no text): {p_stats.skipped_no_text}")
        print(f"Classified: {consumer_stats.classified}")
        print(f"Errors: {consumer_stats.errors}")
        print(f"Skipped: {consumer_stats.skipped}")

    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

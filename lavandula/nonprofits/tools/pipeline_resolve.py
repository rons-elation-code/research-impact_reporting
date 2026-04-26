"""CLI entry point for pipeline URL resolution (Spec 0018).

Usage:
    python -m lavandula.nonprofits.tools.pipeline_resolve --state TX [OPTIONS]

    # Local Ollama (default):
    python -m lavandula.nonprofits.tools.pipeline_resolve --state TX

    # DeepSeek API:
    python -m lavandula.nonprofits.tools.pipeline_resolve --state TX \
        --llm-url https://api.deepseek.com/v1 \
        --llm-model deepseek-v4-flash \
        --llm-api-key-ssm lavandula/deepseek/api_key
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from lavandula.common.db import MIN_SCHEMA_VERSION, assert_schema_at_least, make_app_engine
from lavandula.common.secrets import SecretUnavailable, get_brave_api_key
from lavandula.nonprofits.brave_search import BraveRateLimiter
from lavandula.nonprofits.gemma_client import LLMClient
from lavandula.nonprofits.pipeline_resolver import (
    PipelineQueue,
    ShutdownFlag,
    consumer,
    install_sigint_handler,
    load_unresolved_orgs,
    producer,
    run_dry,
)

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipeline_resolve",
        description="Resolve nonprofit website URLs via Brave Search + LLM.",
    )
    p.add_argument("--state", required=True, help="Filter to orgs in this state")
    p.add_argument("--limit", type=int, default=0, help="Max orgs to process (0 = no limit)")
    p.add_argument("--status-filter", default="unresolved", help="Which resolver_status to re-process")
    p.add_argument("--fresh-only", action="store_true", help="Only process orgs with resolver_status IS NULL (skip previously attempted)")
    p.add_argument("--brave-qps", type=float, default=1.0, help="Brave API queries per second")
    p.add_argument("--search-parallelism", type=int, default=4, help="Concurrent Brave search requests")
    p.add_argument("--fetch-parallelism", type=int, default=8, help="Concurrent HTTP fetch requests")
    p.add_argument("--queue-size", type=int, default=32, help="Bounded queue capacity")
    p.add_argument("--llm-url", default="http://localhost:11434/v1", help="OpenAI-compatible endpoint")
    p.add_argument("--llm-model", default="gemma4:e4b", help="Model name/tag")
    p.add_argument("--llm-api-key-ssm", default=None, help="SSM path for API key (omit for local Ollama)")
    p.add_argument("--consumer-threads", type=int, default=1, help="Parallel LLM consumer threads (default: 1)")
    p.add_argument("--dry-run", action="store_true", help="Search + fetch but skip LLM and DB writes")
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.brave_qps <= 0:
        parser.error("--brave-qps must be > 0")

    api_key_value = None
    if args.llm_api_key_ssm:
        from lavandula.common.secrets import get_secret
        api_key_value = get_secret(args.llm_api_key_ssm)

    llm = LLMClient(
        base_url=args.llm_url, model=args.llm_model, api_key=api_key_value,
    )
    log.info("LLM: %s model=%s method=%s", args.llm_url, args.llm_model, llm.method)
    if not args.dry_run and not llm.health_check():
        print(
            f"ERROR: LLM endpoint unreachable at {args.llm_url}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        api_key = get_brave_api_key()
    except SecretUnavailable as exc:
        print(f"ERROR: Brave API key unavailable: {exc}", file=sys.stderr)
        sys.exit(1)

    engine = make_app_engine()
    assert_schema_at_least(engine, MIN_SCHEMA_VERSION)

    try:
        rate_limiter = BraveRateLimiter(args.brave_qps)
        limit = args.limit if args.limit > 0 else None

        orgs = load_unresolved_orgs(
            engine,
            state=args.state,
            limit=limit,
            status_filter=args.status_filter,
            fresh_only=args.fresh_only,
        )
        log.info("Loaded %d orgs to process", len(orgs))

        if not orgs:
            print("No orgs to process.")
            return

        if args.dry_run:
            run_dry(
                orgs,
                api_key=api_key,
                rate_limiter=rate_limiter,
                search_parallelism=args.search_parallelism,
                fetch_parallelism=args.fetch_parallelism,
            )
            return

        pq = PipelineQueue(maxsize=args.queue_size)
        shutdown = ShutdownFlag()
        install_sigint_handler(shutdown)

        t_start = time.monotonic()

        n_consumers = max(1, args.consumer_threads)
        producer_stats = [None]

        def _run_producer():
            producer_stats[0] = producer(
                orgs,
                pq=pq,
                engine=engine,
                api_key=api_key,
                rate_limiter=rate_limiter,
                search_parallelism=args.search_parallelism,
                fetch_parallelism=args.fetch_parallelism,
                shutdown=shutdown,
                method=llm.method,
                n_consumers=n_consumers,
            )

        producer_thread = threading.Thread(target=_run_producer, daemon=True)
        producer_thread.start()
        counter = [0]
        counter_lock = threading.Lock()

        if n_consumers == 1:
            consumer_results = [consumer(
                pq=pq, gemma=llm, engine=engine, shutdown=shutdown,
                counter=counter, counter_lock=counter_lock,
            )]
        else:
            consumer_results = [None] * n_consumers

            def _run_consumer(idx):
                consumer_results[idx] = consumer(
                    pq=pq, gemma=llm, engine=engine, shutdown=shutdown,
                    counter=counter, counter_lock=counter_lock,
                )

            consumer_threads = []
            for i in range(n_consumers):
                t = threading.Thread(target=_run_consumer, args=(i,), daemon=True)
                t.start()
                consumer_threads.append(t)
            for t in consumer_threads:
                t.join()

        producer_thread.join(timeout=10)

        wall_time = time.monotonic() - t_start
        p_stats = producer_stats[0]

        resolved = sum(s.resolved for s in consumer_results if s)
        ambiguous = sum(s.ambiguous for s in consumer_results if s)
        unresolved = sum(s.unresolved for s in consumer_results if s)
        errors = sum(s.errors for s in consumer_results if s)

        print("\n--- Pipeline Summary ---")
        print(f"Wall time: {wall_time:.1f}s")
        print(f"Consumer threads: {n_consumers}")
        if p_stats:
            print(f"Brave queries: {p_stats.searched}")
            print(f"Enqueued: {p_stats.enqueued}")
            print(f"Skipped (no results): {p_stats.skipped_no_results}")
            print(f"Skipped (all blocked): {p_stats.skipped_all_blocked}")
            print(f"Skipped (no live): {p_stats.skipped_no_live}")
            print(f"Brave errors: {p_stats.brave_errors}")
        print(f"Resolved: {resolved}")
        print(f"Ambiguous: {ambiguous}")
        print(f"Unresolved: {unresolved}")
        print(f"Errors: {errors}")
        total = resolved + ambiguous + unresolved
        if wall_time > 0 and total > 0:
            print(f"Rate: {total / wall_time * 60:.1f} orgs/minute")

    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

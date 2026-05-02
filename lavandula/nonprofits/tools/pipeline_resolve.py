"""CLI entry point for pipeline URL resolution (Spec 0018, 0031).

Usage:
    python -m lavandula.nonprofits.tools.pipeline_resolve --state TX [OPTIONS]

    # Serpex backend (default, Spec 0031):
    python -m lavandula.nonprofits.tools.pipeline_resolve --state TX \
        --search-backend serpex --search-engines brave

    # Multi-engine:
    python -m lavandula.nonprofits.tools.pipeline_resolve --state TX \
        --search-engines brave,google

    # Brave direct (legacy):
    python -m lavandula.nonprofits.tools.pipeline_resolve --state TX \
        --search-backend brave-direct
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from lavandula.common.db import MIN_SCHEMA_VERSION, assert_schema_at_least, make_app_engine
from lavandula.common.secrets import SecretUnavailable, get_brave_api_key, get_serpex_api_key
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
from lavandula.nonprofits.web_search import (
    RateLimiter,
    SearchConfig,
    get_search_stats,
    reset_search_stats,
    validate_engines,
)

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipeline_resolve",
        description="Resolve nonprofit website URLs via search + LLM.",
    )
    p.add_argument("--state", required=True, help="Filter to orgs in this state")
    p.add_argument("--limit", type=int, default=0, help="Max orgs to process (0 = no limit)")
    p.add_argument("--status-filter", default="unresolved", help="Which resolver_status to re-process")
    p.add_argument("--fresh-only", action="store_true", help="Only process orgs with resolver_status IS NULL (skip previously attempted)")

    # Search backend (Spec 0031)
    p.add_argument("--search-backend", choices=["serpex", "brave-direct"], default="serpex",
                    help="Search backend (default: serpex)")
    p.add_argument("--search-engines", default="brave",
                    help="Comma-separated engines: brave,google,bing,auto (default: brave)")
    p.add_argument("--search-qps", type=float, default=None, help="Search queries per second")
    p.add_argument("--brave-qps", type=float, default=None,
                    help="(deprecated, use --search-qps) Brave API queries per second")

    serpex_key_group = p.add_mutually_exclusive_group()
    serpex_key_group.add_argument("--serpex-api-key", default=None, help="Serpex API key (literal)")
    serpex_key_group.add_argument("--serpex-ssm-key", default=None, help="SSM path for Serpex API key")

    p.add_argument("--search-parallelism", type=int, default=4, help="Concurrent search requests")
    p.add_argument("--fetch-parallelism", type=int, default=8, help="Concurrent HTTP fetch requests")
    p.add_argument("--queue-size", type=int, default=32, help="Bounded queue capacity")
    p.add_argument("--llm-url", default="https://api.deepseek.com/v1", help="OpenAI-compatible endpoint")
    p.add_argument("--llm-model", default="deepseek-v4-flash", help="Model name/tag")
    p.add_argument("--llm-api-key-ssm", default="lavandula/deepseek/api_key", help="SSM path for API key")
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

    # QPS: --search-qps takes priority, --brave-qps is deprecated alias
    qps = args.search_qps or args.brave_qps or 1.0
    if qps <= 0:
        parser.error("--search-qps must be > 0")

    # Engine validation
    if args.search_backend == "brave-direct":
        engines = ["brave"]
    else:
        try:
            engines = validate_engines(args.search_engines.split(","))
        except ValueError as exc:
            parser.error(str(exc))

    # LLM setup
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

    # Search API key
    try:
        if args.search_backend == "serpex":
            if args.serpex_api_key:
                search_api_key = args.serpex_api_key
            elif args.serpex_ssm_key:
                from lavandula.common.secrets import get_secret
                search_api_key = get_secret(args.serpex_ssm_key)
            else:
                search_api_key = get_serpex_api_key()
        else:
            search_api_key = get_brave_api_key()
    except SecretUnavailable as exc:
        print(f"ERROR: Search API key unavailable: {exc}", file=sys.stderr)
        sys.exit(1)

    search_config = SearchConfig(
        backend=args.search_backend,
        engines=engines,
        api_key=search_api_key,
        qps=qps,
    )
    rate_limiter = RateLimiter(qps)

    log.info(
        "Search: backend=%s engines=%s qps=%.1f",
        args.search_backend, ",".join(engines), qps,
    )

    engine = make_app_engine()
    assert_schema_at_least(engine, MIN_SCHEMA_VERSION)

    reset_search_stats()

    try:
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
                search_config=search_config,
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
                search_config=search_config,
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
            print(f"Search queries: {p_stats.searched}")
            print(f"Enqueued: {p_stats.enqueued}")
            print(f"Skipped (no results): {p_stats.skipped_no_results}")
            print(f"Skipped (all blocked): {p_stats.skipped_all_blocked}")
            print(f"Skipped (no live): {p_stats.skipped_no_live}")
            print(f"Search errors: {p_stats.search_errors}")
        print(f"Resolved: {resolved}")
        print(f"Ambiguous: {ambiguous}")
        print(f"Unresolved: {unresolved}")
        print(f"Errors: {errors}")
        total = resolved + ambiguous + unresolved
        if wall_time > 0 and total > 0:
            print(f"Rate: {total / wall_time * 60:.1f} orgs/minute")

        # Search stats (Spec 0031)
        s_stats = get_search_stats()
        if s_stats.estimated_credits > 0:
            print("\n--- Search Summary ---")
            eng_parts = ", ".join(f"{k}={v}" for k, v in sorted(s_stats.successful_by_engine.items()))
            print(f"Engine queries: {eng_parts}")
            if s_stats.failed_by_engine:
                fail_parts = ", ".join(f"{k}={v}" for k, v in sorted(s_stats.failed_by_engine.items()))
                print(f"Engine failures: {fail_parts}")
            print(f"Search: {s_stats.search_full} full, {s_stats.search_partial} partial, {s_stats.search_failed} failed")
            print(f"Estimated credits: {s_stats.estimated_credits}")

    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

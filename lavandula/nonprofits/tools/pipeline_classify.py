"""CLI entry point for pipeline report classification (Spec 0018).

Usage:
    python -m lavandula.nonprofits.tools.pipeline_classify [OPTIONS]

    # DeepSeek API:
    python -m lavandula.nonprofits.tools.pipeline_classify \
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
from lavandula.nonprofits.definition_loader import resolve_definition_name
from lavandula.nonprofits.gemma_client import LLMClient
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
        description="Classify nonprofit reports via LLM.",
    )
    p.add_argument("--limit", type=int, default=0, help="Max reports to process (0 = no limit)")
    p.add_argument("--queue-size", type=int, default=32, help="Bounded queue capacity")
    p.add_argument("--llm-url", default="https://api.deepseek.com/v1", help="OpenAI-compatible endpoint")
    p.add_argument("--llm-model", default="deepseek-v4-flash", help="Model name/tag")
    p.add_argument("--llm-api-key-ssm", default="lavandula/deepseek/api_key", help="SSM path for API key")
    p.add_argument("--state", default=None, help="Only classify corpus rows from orgs in this state (e.g. TX)")
    p.add_argument("--re-classify", action="store_true", help="Re-classify all rows, not just NULL classification")
    p.add_argument("--definition", default=None,
                   help="Definition file name (default: env LAVANDULA_CLASSIFIER_DEFINITION or corpus_reports)")
    p.add_argument("--re-classify-definition", default=None,
                   help="Re-classify rows where classifier_definition != this value "
                   "(e.g., corpus_reports:v2). Uses IS DISTINCT FROM to include NULLs. "
                   "Implies --re-classify.")
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.re_classify_definition:
        args.re_classify = True

    defn_name = resolve_definition_name(args.definition)

    api_key_value = None
    if args.llm_api_key_ssm:
        from lavandula.common.secrets import get_secret
        api_key_value = get_secret(args.llm_api_key_ssm)

    llm = LLMClient(
        base_url=args.llm_url, model=args.llm_model, api_key=api_key_value,
        definition_name=defn_name,
    )
    log.info("LLM: %s model=%s method=%s definition=%s", args.llm_url, args.llm_model, llm.method, defn_name)
    if not llm.health_check():
        print(
            f"ERROR: LLM endpoint unreachable at {args.llm_url}",
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

        cdef = f"{llm.definition.name}:v{llm.definition.version}"

        producer_stats = [None]

        def _run_producer():
            producer_stats[0] = classify_producer(
                engine=engine,
                pq=pq,
                limit=limit,
                shutdown=shutdown,
                method=llm.method,
                state=args.state.upper() if args.state else None,
                re_classify=args.re_classify,
                classifier_definition=cdef,
                re_classify_definition=args.re_classify_definition,
            )

        producer_thread = threading.Thread(target=_run_producer, daemon=True)
        producer_thread.start()

        consumer_stats = classify_consumer(
            pq=pq, gemma=llm, engine=engine, shutdown=shutdown,
            classifier_definition=cdef,
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

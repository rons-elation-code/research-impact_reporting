"""Producer-consumer pipeline for report classification via Gemma (Spec 0018).

Same queue architecture as the resolver pipeline but with a different data
source (reports table) and Gemma call (classification instead of disambiguation).
"""
from __future__ import annotations

import logging
import queue
import time
from dataclasses import dataclass

import requests as http_requests
from sqlalchemy import text
from sqlalchemy.engine import Engine

from .gemma_client import LLMClient, LLMParseError

GemmaClient = LLMClient
GemmaParseError = LLMParseError
from .pipeline_resolver import PipelineQueue, ShutdownFlag

log = logging.getLogger(__name__)

_SCHEMA = "lava_corpus"
_SENTINEL = None
_RETRY_DELAYS = [5, 10, 20]
_PAGE_SIZE = 100


@dataclass
class ClassifyProducerStats:
    scanned: int = 0
    enqueued: int = 0
    skipped_no_text: int = 0


@dataclass
class ClassifyConsumerStats:
    classified: int = 0
    errors: int = 0
    skipped: int = 0


def classify_producer(
    *,
    engine: Engine,
    pq: PipelineQueue,
    limit: int | None = None,
    shutdown: ShutdownFlag,
    method: str = "",
    state: str | None = None,
    re_classify: bool = False,
    classifier_definition: str = "",
    re_classify_definition: str | None = None,
) -> ClassifyProducerStats:
    """Keyset pagination over reports with NULL classification, enqueue for LLM."""
    stats = ClassifyProducerStats()
    last_cursor = ""
    remaining = limit

    try:
        while True:
            if shutdown.is_set():
                break

            if re_classify_definition:
                null_filter = " AND {p}classifier_definition IS DISTINCT FROM :target_def"
            elif re_classify:
                null_filter = ""
            else:
                null_filter = " AND {p}classification IS NULL"
            if state:
                p = "c."
                sql = (
                    f"SELECT c.content_sha256, c.first_page_text "
                    f"  FROM {_SCHEMA}.corpus c "
                    f"  JOIN {_SCHEMA}.nonprofits_seed ns ON ns.ein = c.source_org_ein "
                    f" WHERE c.content_sha256 > :cursor "
                    f"   AND ns.state = :state "
                    f"{null_filter.format(p=p)} "
                    f" ORDER BY c.content_sha256 LIMIT :page_size"
                )
            else:
                p = ""
                sql = (
                    f"SELECT content_sha256, first_page_text FROM {_SCHEMA}.corpus "
                    f"WHERE content_sha256 > :cursor "
                    f"{null_filter.format(p=p)} "
                    "ORDER BY content_sha256 LIMIT :page_size"
                )
            page_size = _PAGE_SIZE
            if remaining is not None:
                page_size = min(page_size, remaining)

            bind_params: dict = {"cursor": last_cursor, "page_size": page_size}
            if state:
                bind_params["state"] = state
            if re_classify_definition:
                bind_params["target_def"] = re_classify_definition

            with engine.connect() as conn:
                rows = conn.execute(
                    text(sql),
                    bind_params,
                ).fetchall()

            if not rows:
                break

            for content_sha256, first_page_text in rows:
                if shutdown.is_set():
                    break

                stats.scanned += 1
                last_cursor = content_sha256

                if not first_page_text or not first_page_text.strip():
                    stats.skipped_no_text += 1
                    log.info("[skip] sha=%s  no text", content_sha256[:10])
                    try:
                        with engine.begin() as conn:
                            conn.execute(
                                text(
                                    f"UPDATE {_SCHEMA}.corpus SET "
                                    "classification='skipped', "
                                    "classifier_model=:model, "
                                    "classifier_definition=:cdef "
                                    "WHERE content_sha256=:csha"
                                ),
                                {
                                    "model": method,
                                    "cdef": classifier_definition,
                                    "csha": content_sha256,
                                },
                            )
                    except Exception:
                        log.exception("DB write error for content_sha256=%s", content_sha256)
                    continue

                pq.put({"content_sha256": content_sha256, "first_page_text": first_page_text})
                stats.enqueued += 1

                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        break

            if remaining is not None and remaining <= 0:
                break
            if len(rows) < page_size:
                break

    finally:
        pq.done()

    return stats


def classify_consumer(
    *,
    pq: PipelineQueue,
    gemma: LLMClient,
    engine: Engine,
    shutdown: ShutdownFlag,
    classifier_definition: str = "",
) -> ClassifyConsumerStats:
    """Pull report packets from the queue, classify via LLM, write results."""
    stats = ClassifyConsumerStats()
    method = gemma.method

    while True:
        try:
            packet = pq.get(timeout=5.0)
        except queue.Empty:
            if shutdown.is_set():
                break
            continue

        if packet is _SENTINEL:
            break

        content_sha256 = packet["content_sha256"]
        first_page_text = packet["first_page_text"]

        result = None
        for attempt, delay in enumerate(
            [0] + _RETRY_DELAYS, start=1
        ):
            if attempt > 1:
                time.sleep(delay)
            try:
                result = gemma.classify(first_page_text)
                break
            except http_requests.ConnectionError:
                if attempt > len(_RETRY_DELAYS):
                    log.error(
                        "Gemma unreachable after %d attempts for content_sha256=%s",
                        attempt, content_sha256,
                    )
                    result = None
            except GemmaParseError as exc:
                log.warning("Gemma parse error for content_sha256=%s: %s", content_sha256, exc)
                result = {"_parse_error": True}
                break

        if result is None:
            stats.skipped += 1
            continue

        if result.get("_parse_error"):
            stats.errors += 1
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"UPDATE {_SCHEMA}.corpus SET "
                            "classification='parse_error', "
                            "classifier_model=:model, "
                            "classifier_definition=:cdef "
                            "WHERE content_sha256=:csha"
                        ),
                        {
                            "model": method,
                            "cdef": classifier_definition,
                            "csha": content_sha256,
                        },
                    )
            except Exception:
                log.exception("DB write error for content_sha256=%s", content_sha256)
            continue

        classification = result.get("classification", "other")
        confidence = float(result.get("confidence", 0))

        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"UPDATE {_SCHEMA}.corpus SET "
                        "classification=:cls, "
                        "classification_confidence=:conf, "
                        "classifier_model=:model, "
                        "material_type=:mt, "
                        "material_group=:mg, "
                        "event_type=:et, "
                        "reasoning=:reasoning, "
                        "classifier_definition=:cdef "
                        "WHERE content_sha256=:csha"
                    ),
                    {
                        "cls": classification,
                        "conf": confidence,
                        "model": method,
                        "mt": result.get("material_type"),
                        "mg": result.get("material_group"),
                        "et": result.get("event_type"),
                        "reasoning": (result.get("reasoning") or "")[:500],
                        "cdef": result.get("classifier_definition", classifier_definition),
                        "csha": content_sha256,
                    },
                )
            stats.classified += 1
            log.info(
                "[%d] sha=%s  %s (%.2f)",
                stats.classified, content_sha256[:10], classification, confidence,
            )
        except Exception:
            stats.errors += 1
            log.exception("DB write error for content_sha256=%s", content_sha256)

    return stats


__all__ = [
    "ClassifyConsumerStats",
    "ClassifyProducerStats",
    "classify_consumer",
    "classify_producer",
]

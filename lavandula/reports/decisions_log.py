"""Per-candidate decision log — JSONL with daily rotation.

Every candidate-evaluation writes a JSON line to logs/crawler_decisions.jsonl.
URLs are redacted via the existing url_redact module; an allowlist prevents
accidental leakage of future unredacted context.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from . import config
from .url_redact import redact_url

_logger: logging.Logger | None = None

_URL_FIELDS = frozenset({"url", "referring_page"})

_ALLOWED_FIELDS = frozenset({
    "ts", "ein", "url_redacted", "referring_page_redacted",
    "basename", "filename_score", "triage",
    "strong_path_hit", "weak_path_hit",
    "anchor_text", "anchor_hit",
    "decision", "discovered_via",
    # Spec 0022: wayback_query event fields (AC19)
    "event_type", "domain", "outcome", "reason",
    "cdx_http_status", "row_count_raw", "row_count_after_dedup",
    "elapsed_ms", "capture_hosts",
})


def _init() -> logging.Logger:
    logger = logging.getLogger("lavandula.crawler.decisions")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_dir = config.LOGS
    log_dir.mkdir(exist_ok=True)
    handler = TimedRotatingFileHandler(
        filename=log_dir / "crawler_decisions.jsonl",
        when="midnight",
        backupCount=90,
        encoding="utf-8",
        utc=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    if not logger.handlers:
        logger.addHandler(handler)
    return logger


def log_decision(record: dict) -> None:
    """Emit a JSONL decision record. Redacts URL fields and allowlists keys."""
    global _logger
    if _logger is None:
        _logger = _init()
    safe: dict = {}
    for k, v in record.items():
        if k in _URL_FIELDS and isinstance(v, str) and v:
            safe[f"{k}_redacted"] = redact_url(v)
        elif k in _ALLOWED_FIELDS:
            safe[k] = v
    safe.setdefault("ts", datetime.now(timezone.utc).isoformat())
    _logger.info(json.dumps(safe, default=str))

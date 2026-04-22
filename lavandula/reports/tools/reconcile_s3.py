"""Reconcile S3 archive against `lava_impact.reports` (Spec 0007 AC16, Spec 0017).

Detects orphan objects — bytes in S3 whose sha256 has no `reports`
row (e.g. because the crawler crashed between PUT and the DB write).
For each orphan, reads canonical metadata from the S3 object and in
`--apply` mode inserts a minimal `reports` row so the pipeline
regains visibility into the bytes.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from urllib.parse import unquote, urlparse

from sqlalchemy import text
from sqlalchemy.engine import Engine

from lavandula.common.db import (
    MIN_SCHEMA_VERSION,
    assert_schema_at_least,
    make_app_engine,
)

from .. import config
from .. import s3_archive as _s3a
from ..url_redact import redact_url


log = logging.getLogger("lavandula.reports.reconcile_s3")

_SHA_RE = re.compile(r"^[a-f0-9]{64}$")
_KEY_RE = re.compile(r"^(?:.*/)?([a-f0-9]{64})\.pdf$")
_EIN_RE = re.compile(r"^\d{9}$")

_ALLOWED_ATTRIBUTION = {"own_domain", "platform_verified", "platform_unverified"}
_ALLOWED_DISCOVERED_VIA = {
    "sitemap", "homepage-link", "subpage-link", "hosting-platform",
}

_DEFAULT_ATTRIBUTION_ON_RECOVERY = "platform_verified"
_DEFAULT_DISCOVERED_VIA_ON_RECOVERY = "homepage-link"


def _iter_object_shas(client, bucket: str, prefix: str):
    paginator = client.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix.rstrip("/") + "/"
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            m = _KEY_RE.match(key)
            if m:
                yield key, m.group(1)


def _db_shas(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {
            row[0] for row in conn.execute(text(
                "SELECT content_sha256 FROM lava_impact.reports"
            ))
        }


def _fetch_log_attribution(engine: Engine, sha: str) -> tuple[str, str] | None:
    """Look up the authoritative (ein, url_redacted) for `sha` in fetch_log."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT ein, url_redacted FROM lava_impact.fetch_log "
                " WHERE kind = 'pdf-get' AND notes LIKE :pat "
                " ORDER BY id DESC LIMIT 1"
            ),
            {"pat": f"%sha={sha}%"},
        ).fetchone()
    if row is None or not row[0]:
        return None
    return row[0], row[1]


def _valid_metadata(md: dict) -> tuple[bool, dict, str]:
    ein = md.get("ein", "")
    raw_url = md.get("source-url", "")
    fetched_at = md.get("fetched-at", "")

    if not _EIN_RE.match(ein):
        return False, {}, f"bad_ein:{ein!r}"

    url = unquote(raw_url) if raw_url else ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, {}, "bad_source_url"

    if fetched_at:
        try:
            datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except ValueError:
            return False, {}, "bad_fetched_at"

    attribution = md.get("attribution-confidence", "")
    discovered_via = md.get("discovered-via", "")
    if attribution and attribution not in _ALLOWED_ATTRIBUTION:
        attribution = ""
    if discovered_via and discovered_via not in _ALLOWED_DISCOVERED_VIA:
        discovered_via = ""

    return True, {
        "ein": ein,
        "source_url": url,
        "fetched_at": fetched_at,
        "attribution_confidence": attribution,
        "discovered_via": discovered_via,
    }, ""


def _insert_orphan_row(
    engine: Engine,
    *,
    sha: str,
    ein: str,
    source_url: str,
    fetched_at: str,
    file_size_bytes: int,
    attribution_confidence: str,
    discovered_via: str,
) -> None:
    """Minimal row — schema NOT NULLs only. Uses ON CONFLICT DO NOTHING."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO lava_impact.reports ("
                "  content_sha256, source_url_redacted, source_org_ein, "
                "  discovered_via, attribution_confidence, archived_at, "
                "  content_type, file_size_bytes, classifier_model, "
                "  classifier_version, extractor_version"
                ") VALUES ("
                "  :sha, :url, :ein, :disc, :attr, :archived, :ct, :size, "
                "  :model, :cver, :ext"
                ") ON CONFLICT (content_sha256) DO NOTHING"
            ),
            {
                "sha": sha,
                "url": source_url,
                "ein": ein,
                "disc": discovered_via,
                "attr": attribution_confidence,
                "archived": fetched_at or datetime.utcnow()
                    .replace(microsecond=0).isoformat() + "Z",
                "ct": "application/pdf",
                "size": max(int(file_size_bytes or 1), 1),
                "model": config.CLASSIFIER_MODEL,
                "cver": config.CLASSIFIER_VERSION,
                "ext": config.EXTRACTOR_VERSION,
            },
        )


def reconcile(
    *,
    engine: Engine,
    uri: str,
    apply: bool,
    client=None,
) -> int:
    bucket, prefix = _s3a.parse_s3_uri(uri)
    if not prefix:
        prefix = config.DEFAULT_S3_PREFIX

    if client is None:
        archive = _s3a.S3Archive(bucket, prefix)
        client = archive._client  # noqa: SLF001

    db_shas = _db_shas(engine)
    s3_shas: set[str] = set()
    orphans: list[tuple[str, str]] = []
    for key, sha in _iter_object_shas(client, bucket, prefix):
        s3_shas.add(sha)
        if sha not in db_shas:
            orphans.append((key, sha))

    for key, sha in orphans:
        head = client.head_object(Bucket=bucket, Key=key)
        md = head.get("Metadata", {}) or {}
        ok, clean, reason = _valid_metadata(md)
        size = head.get("ContentLength", 0)

        fl = _fetch_log_attribution(engine, sha)
        ein: str | None = None
        source_redacted: str | None = None
        fetched_at = ""
        source_tag = ""

        if fl is not None:
            fl_ein, fl_url = fl
            ein = fl_ein
            source_redacted = fl_url
            source_tag = "fetch_log"
            if ok and clean["ein"] != fl_ein:
                log.warning(
                    "RECONCILE_MISMATCH sha=%s fetch_log_ein=%s "
                    "s3_metadata_ein=%s (using fetch_log)",
                    sha, fl_ein, clean["ein"],
                )
            if ok:
                fetched_at = clean["fetched_at"]
        elif ok:
            log.warning(
                "RECONCILE_S3_ONLY sha=%s (no fetch_log entry; "
                "using S3 metadata — best-effort)",
                sha,
            )
            ein = clean["ein"]
            source_redacted = redact_url(clean["source_url"])
            fetched_at = clean["fetched_at"]
            source_tag = "s3_metadata"
        else:
            print(f"ORPHAN_INVALID_METADATA sha={sha} reason={reason}")
            continue

        md_attr = clean.get("attribution_confidence", "") if ok else ""
        md_disc = clean.get("discovered_via", "") if ok else ""
        attribution = md_attr or _DEFAULT_ATTRIBUTION_ON_RECOVERY
        discovered_via = md_disc or _DEFAULT_DISCOVERED_VIA_ON_RECOVERY
        if not md_attr:
            log.warning(
                "RECONCILE_DEFAULT_ATTRIBUTION sha=%s using=%s "
                "(no attribution-confidence in S3 metadata)",
                sha, attribution,
            )

        if apply:
            _insert_orphan_row(
                engine,
                sha=sha,
                ein=ein,
                source_url=source_redacted,
                fetched_at=fetched_at,
                file_size_bytes=size,
                attribution_confidence=attribution,
                discovered_via=discovered_via,
            )
            print(
                f"ORPHAN_APPLIED sha={sha} ein={ein} "
                f"source={source_redacted} attribution_src={source_tag} "
                f"attribution={attribution}"
            )
        else:
            print(
                f"ORPHAN sha={sha} ein={ein} "
                f"source={source_redacted} attribution_src={source_tag} "
                f"attribution={attribution}"
            )

    missing = db_shas - s3_shas
    for sha in missing:
        print(f"MISSING sha={sha}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconcile S3 archive with lava_impact.reports")
    ap.add_argument("--archive", required=True, help="s3://bucket/prefix")
    mx = ap.add_mutually_exclusive_group(required=True)
    mx.add_argument("--dry-run", action="store_true")
    mx.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    engine = make_app_engine()
    assert_schema_at_least(engine, MIN_SCHEMA_VERSION)
    try:
        return reconcile(
            engine=engine,
            uri=args.archive,
            apply=bool(args.apply),
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

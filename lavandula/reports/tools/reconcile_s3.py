"""Reconcile S3 archive against reports.db (spec 0007 AC16).

Detects orphan objects — bytes in S3 whose sha256 has no `reports`
row (e.g. because the crawler crashed between PUT and the queued DB
write). For each orphan, reads the canonical metadata (`ein`,
`source-url`, `fetched-at`) from the S3 object and in `--apply` mode
inserts a minimal `reports` row so the pipeline regains visibility
into the bytes.

Usage:
    python -m lavandula.reports.tools.reconcile_s3 \\
        --db /path/to/reports.db \\
        --archive s3://bucket/prefix \\
        --dry-run | --apply
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from datetime import datetime
from urllib.parse import unquote, urlparse

from .. import config
from .. import s3_archive as _s3a


log = logging.getLogger("lavandula.reports.reconcile_s3")

_SHA_RE = re.compile(r"^[a-f0-9]{64}$")
_KEY_RE = re.compile(r"^(?:.*/)?([a-f0-9]{64})\.pdf$")
_EIN_RE = re.compile(r"^\d{9}$")


def _iter_object_shas(client, bucket: str, prefix: str):
    """Yield (key, sha256) pairs for PDFs under prefix."""
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


def _db_shas(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute(
            "SELECT content_sha256 FROM reports"
        )
    }


def _valid_metadata(md: dict) -> tuple[bool, dict, str]:
    """Validate S3 user-metadata from head_object. Returns (ok, clean, reason).

    clean contains: ein, source_url (decoded), fetched_at.
    """
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
            # Accept both the canonical `...Z` and `+00:00` forms.
            datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except ValueError:
            return False, {}, "bad_fetched_at"

    return True, {"ein": ein, "source_url": url, "fetched_at": fetched_at}, ""


def _insert_orphan_row(
    conn: sqlite3.Connection,
    *,
    sha: str,
    ein: str,
    source_url: str,
    fetched_at: str,
    file_size_bytes: int,
) -> None:
    """Minimal row — schema NOT NULLs only. classify_null.py backfills
    the classification columns later; pdf_* flags default to 0."""
    # AC16: reconciler is intentionally the ONLY non-db_writer path that
    # inserts directly into `reports`. It uses ?-parameterized SQL.
    conn.execute(
        """
        INSERT OR IGNORE INTO reports (
          content_sha256, source_url_redacted, source_org_ein,
          discovered_via, attribution_confidence,
          archived_at, content_type, file_size_bytes,
          classifier_model, classifier_version, extractor_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sha,
            source_url,
            ein,
            "homepage-link",
            "platform_unverified",
            fetched_at or datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "application/pdf",
            max(int(file_size_bytes or 1), 1),
            config.CLASSIFIER_MODEL,
            config.CLASSIFIER_VERSION,
            config.EXTRACTOR_VERSION,
        ),
    )


def reconcile(
    *,
    db_path: str,
    uri: str,
    apply: bool,
    client=None,
) -> int:
    """Run the reconciliation. Returns exit code."""
    bucket, prefix = _s3a.parse_s3_uri(uri)
    if not prefix:
        prefix = config.DEFAULT_S3_PREFIX

    if client is None:
        archive = _s3a.S3Archive(bucket, prefix)
        client = archive._client  # noqa: SLF001 — intentional: reuse the client

    conn = sqlite3.connect(db_path)
    try:
        db_shas = _db_shas(conn)
        s3_shas: set[str] = set()
        orphans: list[tuple[str, str]] = []  # (key, sha)
        for key, sha in _iter_object_shas(client, bucket, prefix):
            s3_shas.add(sha)
            if sha not in db_shas:
                orphans.append((key, sha))

        for key, sha in orphans:
            head = client.head_object(Bucket=bucket, Key=key)
            md = head.get("Metadata", {}) or {}
            ok, clean, reason = _valid_metadata(md)
            if not ok:
                print(f"ORPHAN_INVALID_METADATA sha={sha} reason={reason}")
                continue
            size = head.get("ContentLength", 0)
            if apply:
                _insert_orphan_row(
                    conn,
                    sha=sha,
                    ein=clean["ein"],
                    source_url=clean["source_url"],
                    fetched_at=clean["fetched_at"],
                    file_size_bytes=size,
                )
                conn.commit()
                print(
                    f"ORPHAN_APPLIED sha={sha} ein={clean['ein']} "
                    f"source={clean['source_url']}"
                )
            else:
                print(
                    f"ORPHAN sha={sha} ein={clean['ein']} "
                    f"source={clean['source_url']}"
                )

        missing = db_shas - s3_shas
        for sha in missing:
            print(f"MISSING sha={sha}")
    finally:
        conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconcile S3 archive with reports.db")
    ap.add_argument("--db", required=True, help="Path to reports.db")
    ap.add_argument("--archive", required=True, help="s3://bucket/prefix")
    mx = ap.add_mutually_exclusive_group(required=True)
    mx.add_argument("--dry-run", action="store_true")
    mx.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    return reconcile(
        db_path=args.db,
        uri=args.archive,
        apply=bool(args.apply),
    )


if __name__ == "__main__":
    sys.exit(main())

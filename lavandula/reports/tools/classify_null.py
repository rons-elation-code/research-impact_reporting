"""Batch-classify all reports where classification IS NULL.

Iterates the `reports` table of an existing reports.db, calls the
selected classifier backend on each row's `first_page_text`, writes
the result back in-place.

Usage:
    CLASSIFIER_CLIENT=codex python -m lavandula.reports.tools.classify_null \\
        --db /tmp/0004-coastal-run/data/reports.db

Idempotent — re-runs only touch rows still NULL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

from lavandula.reports.classifier_clients import (
    CodexSubscriptionClient,
    select_classifier_client,
)
from lavandula.reports.classify import (
    ClassifierError,
    classify_first_page,
)


def _effective_classifier_model(client, result) -> str:
    """Return a truthful classifier_model string for the audit trail.

    The shim's `messages.create()` call discards the `model` kwarg
    passed in by `classify.build_anthropic_kwargs()` because Codex
    CLI uses its own config. Without this override, the DB would
    record the unused Anthropic model name (claude-haiku-4-5)
    instead of the actual Codex model that ran.
    """
    if isinstance(client, CodexSubscriptionClient):
        # _codex_model may be None when using codex CLI's default
        return f"codex-cli/{client._codex_model or 'default'}"
    return result.classifier_model


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after N classifications (for testing).")
    ap.add_argument("--sha-prefix", type=str, default=None,
                    help="Only classify rows whose sha256 starts with this "
                    "prefix (for testing).")
    ap.add_argument("--re-classify", action="store_true",
                    help="Re-classify rows that already have a classification "
                    "(used to re-stamp with new model). WARNING: overwrites.")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"error: {args.db} not found", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    # Select rows needing classification
    null_filter = "" if args.re_classify else "AND classification IS NULL"
    sql = f"""
        SELECT content_sha256, first_page_text
        FROM reports
        WHERE first_page_text IS NOT NULL
          AND first_page_text != ''
          {null_filter}
    """
    params: tuple = ()
    if args.sha_prefix:
        sql += " AND content_sha256 LIKE ?"
        params = (args.sha_prefix + "%",)
    sql += " ORDER BY archived_at"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    rows = conn.execute(sql, params).fetchall()
    total = len(rows)
    print(f"classifying {total} rows from {args.db}")
    if total == 0:
        return 0

    client = select_classifier_client()
    print(f"client: {type(client).__name__}\n")

    ok = 0
    errs = 0
    unknown_enum = 0
    low_confidence = 0
    classification_counts: dict[str, int] = {}

    for i, row in enumerate(rows, 1):
        sha = row["content_sha256"]
        text = row["first_page_text"]
        try:
            result = classify_first_page(
                text, client=client, raise_on_error=False
            )
        except ClassifierError as exc:
            # Schema-violation exceptions still propagate here
            unknown_enum += 1
            print(f"  [{i:>3}/{total}] sha={sha[:10]}  SCHEMA ERROR: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            errs += 1
            print(f"  [{i:>3}/{total}] sha={sha[:10]}  UNEXPECTED: {type(exc).__name__}: {exc}")
            continue

        if result.classification is None:
            errs += 1
            print(f"  [{i:>3}/{total}] sha={sha[:10]}  NULL (error: {result.error})")
            continue

        # Count by classification
        classification_counts[result.classification] = \
            classification_counts.get(result.classification, 0) + 1
        if result.classification_confidence is not None and \
                result.classification_confidence < 0.8:
            low_confidence += 1

        # Write back with truthful provenance
        conn.execute(
            """UPDATE reports
               SET classification=?,
                   classification_confidence=?,
                   classifier_model=?,
                   classifier_version=?,
                   classified_at=?
               WHERE content_sha256=?""",
            (
                result.classification,
                result.classification_confidence,
                _effective_classifier_model(client, result),
                1,
                iso_now(),
                sha,
            ),
        )
        conn.commit()
        ok += 1
        print(f"  [{i:>3}/{total}] sha={sha[:10]}  {result.classification:<12} conf={result.classification_confidence:.2f}")

    print(f"\n=== done ===")
    print(f"  classified ok:   {ok}")
    print(f"  null (errors):   {errs}")
    print(f"  schema errors:   {unknown_enum}")
    print(f"  low confidence:  {low_confidence}")
    print(f"\n=== by classification ===")
    for cls in sorted(classification_counts.keys()):
        print(f"  {cls:<14} {classification_counts[cls]}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

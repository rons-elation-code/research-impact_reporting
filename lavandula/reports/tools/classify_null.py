"""Batch-classify all reports where classification IS NULL.

Iterates `lava_impact.reports`, calls the selected classifier backend
on each row's `first_page_text`, and writes the result back in-place
via the SQLAlchemy engine (Spec 0017).

Usage:
    python -m lavandula.reports.tools.classify_null --max-workers 4

Idempotent — re-runs only touch rows still NULL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import text
from sqlalchemy.engine import Engine

from lavandula.common.db import (
    MIN_SCHEMA_VERSION,
    assert_schema_at_least,
    make_app_engine,
)
from lavandula.reports import budget, config, db_writer
from lavandula.reports.classifier_clients import (
    CodexSubscriptionClient,
    select_classifier_client,
)
from lavandula.reports.classify import (
    ClassifierError,
    classify_first_page,
    estimate_cents,
)


class _BudgetHalt(SystemExit):
    """Raised internally to halt the run when the budget cap is hit."""
    def __init__(self, message: str):
        super().__init__(2)
        self.message = message


# --- Subprocess tracking (TICK-002 Ctrl-C cleanup) ---------------------

_ACTIVE_PROCS: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def _tracking_subprocess_run(
    cmd,
    *,
    input=None,
    capture_output=False,
    timeout=None,
    text=False,
    check=False,
    env=None,
    **kwargs,
):
    """Drop-in replacement for subprocess.run that tracks active Popens."""
    stdin = subprocess.PIPE if input is not None else None
    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None
    proc = subprocess.Popen(
        cmd, stdin=stdin, stdout=stdout, stderr=stderr,
        text=text, env=env, **kwargs,
    )
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.add(proc)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_PROCS.discard(proc)
    cp = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return cp


def kill_active_subprocesses() -> int:
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS)
    n = 0
    for p in procs:
        try:
            p.kill()
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def _effective_classifier_model(client, result) -> str:
    if isinstance(client, CodexSubscriptionClient):
        return f"codex-cli/{client._codex_model or 'default'}"
    return result.classifier_model


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# Per-thread classifier client (TICK-002 round-3 review).
_classifier_local = threading.local()
_classifier_factory = None  # set by main() before dispatch


def _get_thread_classifier():
    c = getattr(_classifier_local, "client", None)
    if c is None:
        c = _classifier_factory()
        if isinstance(c, CodexSubscriptionClient):
            c._runner = _tracking_subprocess_run
        _classifier_local.client = c
    return c


def _classify_one(sha: str, text_input: str, *, engine: Engine,
                  budget_enabled=False, halt_event=None):
    """Worker-side classify call. Returns (sha, (kind, payload))."""
    if halt_event is not None and halt_event.is_set():
        return sha, ("cancelled", None)

    client = _get_thread_classifier()
    reservation_id = None
    if budget_enabled:
        try:
            est = estimate_cents(1200, 150)
            reservation_id = budget.check_and_reserve(
                engine,
                estimated_cents=est,
                classifier_model=config.CLASSIFIER_MODEL,
            )
        except budget.BudgetExceeded as exc:
            if halt_event is not None:
                halt_event.set()
            return sha, ("budget_halt", exc)
        except Exception as exc:  # noqa: BLE001
            return sha, ("budget_error", exc)

    try:
        result = classify_first_page(
            text_input, client=client, raise_on_error=False
        )
    except ClassifierError as exc:
        _release_reservation(engine, reservation_id)
        return sha, ("schema_error", exc)
    except Exception as exc:  # noqa: BLE001
        _release_reservation(engine, reservation_id)
        return sha, ("unexpected", exc)

    if budget_enabled and reservation_id is not None:
        if result.classification is None:
            _release_reservation(engine, reservation_id)
        else:
            try:
                budget.settle(
                    engine,
                    reservation_id=reservation_id,
                    actual_input_tokens=getattr(result, "input_tokens", 0) or 0,
                    actual_output_tokens=getattr(result, "output_tokens", 0) or 0,
                    sha256_classified=sha,
                )
            except Exception:  # noqa: BLE001
                _release_reservation(engine, reservation_id)
                raise

    return sha, ("ok", result)


def _release_reservation(engine: Engine, reservation_id) -> None:
    if reservation_id is None:
        return
    try:
        budget.release(engine, reservation_id=reservation_id)
    except Exception:  # noqa: BLE001,S110  # nosec B110 — best-effort rollback
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after N classifications (for testing).")
    ap.add_argument("--sha-prefix", type=str, default=None,
                    help="Only classify rows whose sha256 starts with this "
                    "prefix (for testing).")
    ap.add_argument("--re-classify", action="store_true",
                    help="Re-classify rows that already have a classification.")
    ap.add_argument("--max-workers", type=int, default=4,
                    help="Parallel classifier threads (TICK-002). Default 4.")
    args = ap.parse_args()

    if args.max_workers < 1 or args.max_workers > 32:
        ap.error("--max-workers must be between 1 and 32")

    engine = make_app_engine()
    assert_schema_at_least(engine, MIN_SCHEMA_VERSION)

    null_filter = "" if args.re_classify else " AND classification IS NULL "
    sql = (
        "SELECT content_sha256, first_page_text, "
        "       source_org_ein, source_url_redacted "
        "  FROM lava_impact.reports "
        " WHERE first_page_text IS NOT NULL "
        "   AND first_page_text <> '' "
        f"{null_filter}"
    )
    params: dict = {}
    if args.sha_prefix:
        sql += " AND content_sha256 LIKE :prefix "
        params["prefix"] = args.sha_prefix + "%"
    sql += " ORDER BY archived_at"
    if args.limit:
        sql += " LIMIT :limit"
        params["limit"] = int(args.limit)

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    total = len(rows)
    print(f"classifying {total} rows (max_workers={args.max_workers})")
    if total == 0:
        return 0

    global _classifier_factory
    _classifier_factory = select_classifier_client
    if hasattr(_classifier_local, "client"):
        del _classifier_local.client
    sample_client = _get_thread_classifier()
    print(f"client: {type(sample_client).__name__}\n")

    ok = 0
    errs = 0
    unknown_enum = 0
    low_confidence = 0
    classification_counts: dict[str, int] = {}

    budget_enabled = True
    try:
        reclaimed = budget.reconcile_stale_reservations(engine)
        if reclaimed:
            print(f"reconciled {reclaimed} stale classifier preflight reservation(s)")
    except Exception as exc:  # noqa: BLE001
        budget_enabled = False
        print(f"note: budget ledger unavailable ({exc}); skipping accounting",
              file=sys.stderr)

    halt_event = threading.Event()
    halt_message = {"text": ""}

    def _log_classify_event(sha: str, ein: str | None,
                            url_redacted: str, fetch_status: str,
                            notes: str = "") -> None:
        try:
            db_writer.record_fetch(
                engine,
                ein=ein,
                url_redacted=url_redacted or "",
                kind="classify",
                fetch_status=fetch_status,
                notes=notes or None,
            )
        except Exception:  # noqa: BLE001
            pass

    def _write_result(sha: str, result) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE lava_impact.reports SET "
                    "  classification = :class, "
                    "  classification_confidence = :conf, "
                    "  classifier_model = :model, "
                    "  classifier_version = :cver, "
                    "  classified_at = :ts "
                    "WHERE content_sha256 = :sha"
                ),
                {
                    "class": result.classification,
                    "conf": result.classification_confidence,
                    "model": _effective_classifier_model(sample_client, result),
                    "cver": 1,
                    "ts": iso_now(),
                    "sha": sha,
                },
            )

    executor = ThreadPoolExecutor(
        max_workers=args.max_workers,
        thread_name_prefix="classify-null",
    )
    try:
        sha_meta = {
            row["content_sha256"]: (
                row.get("source_org_ein"),
                row.get("source_url_redacted") or "",
            )
            for row in rows
        }
        futures = {
            executor.submit(
                _classify_one,
                row["content_sha256"],
                row["first_page_text"],
                engine=engine,
                budget_enabled=budget_enabled,
                halt_event=halt_event,
            ): i
            for i, row in enumerate(rows, 1)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                sha, (kind, payload) = fut.result()
            except Exception as exc:  # noqa: BLE001
                errs += 1
                print(f"  [{i:>3}/{total}] worker crash: {type(exc).__name__}: {exc}")
                continue

            ein, url_redacted = sha_meta.get(sha, (None, ""))

            if kind == "schema_error":
                unknown_enum += 1
                print(f"  [{i:>3}/{total}] sha={sha[:10]}  SCHEMA ERROR: {payload}")
                _log_classify_event(sha, ein, url_redacted,
                                    "classifier_error", f"schema:{payload}")
                continue
            if kind == "unexpected":
                errs += 1
                print(f"  [{i:>3}/{total}] sha={sha[:10]}  UNEXPECTED: "
                      f"{type(payload).__name__}: {payload}")
                _log_classify_event(sha, ein, url_redacted,
                                    "classifier_error",
                                    f"{type(payload).__name__}")
                continue
            if kind == "budget_halt":
                halt_message["text"] = str(payload)
                print(f"  [{i:>3}/{total}] sha={sha[:10]}  BUDGET HALT: {payload}",
                      file=sys.stderr)
                continue
            if kind == "budget_error":
                errs += 1
                print(f"  [{i:>3}/{total}] sha={sha[:10]}  BUDGET ERROR: "
                      f"{type(payload).__name__}: {payload}",
                      file=sys.stderr)
                continue
            if kind == "cancelled":
                continue

            result = payload
            if result.classification is None:
                errs += 1
                print(f"  [{i:>3}/{total}] sha={sha[:10]}  NULL (error: {result.error})")
                _log_classify_event(sha, ein, url_redacted,
                                    "classifier_error",
                                    str(result.error)[:200])
                continue

            classification_counts[result.classification] = (
                classification_counts.get(result.classification, 0) + 1
            )
            if (result.classification_confidence is not None
                    and result.classification_confidence < 0.8):
                low_confidence += 1

            _write_result(sha, result)
            ok += 1
            _log_classify_event(sha, ein, url_redacted, "ok",
                                f"{result.classification}:"
                                f"{result.classification_confidence:.2f}")
            print(f"  [{i:>3}/{total}] sha={sha[:10]}  "
                  f"{result.classification:<12} "
                  f"conf={result.classification_confidence:.2f}")
        if halt_event.is_set():
            executor.shutdown(wait=False, cancel_futures=True)
    except KeyboardInterrupt:
        print("\n^C — cancelling pending classifications", file=sys.stderr)
        executor.shutdown(wait=False, cancel_futures=True)
        killed = kill_active_subprocesses()
        if killed:
            print(f"killed {killed} in-flight codex subprocess(es)",
                  file=sys.stderr)
        raise
    else:
        executor.shutdown(wait=True)

    # Backfill crawled_orgs.confirmed_report_count from the now-classified
    # reports table.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE lava_impact.crawled_orgs "
                "   SET confirmed_report_count = ( "
                "       SELECT COUNT(*) FROM lava_impact.reports "
                "        WHERE reports.source_org_ein = crawled_orgs.ein "
                "          AND reports.classification IN "
                "              ('annual','impact','hybrid')"
                "   )"
            ))
        print("\nbackfilled crawled_orgs.confirmed_report_count")
    except Exception as exc:  # noqa: BLE001
        print(f"\nwarning: confirmed_report_count backfill failed: {exc}",
              file=sys.stderr)

    print("\n=== done ===")
    print(f"  classified ok:   {ok}")
    print(f"  null (errors):   {errs}")
    print(f"  schema errors:   {unknown_enum}")
    print(f"  low confidence:  {low_confidence}")
    print("\n=== by classification ===")
    for cls in sorted(classification_counts.keys()):
        print(f"  {cls:<14} {classification_counts[cls]}")

    engine.dispose()
    if halt_event.is_set():
        print(f"\nHALT: classifier budget cap exceeded — {halt_message['text']}",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

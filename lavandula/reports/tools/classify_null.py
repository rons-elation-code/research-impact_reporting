"""Batch-classify all reports where classification IS NULL.

Iterates the `reports` table of an existing reports.db, calls the
selected classifier backend on each row's `first_page_text`, writes
the result back in-place.

TICK-002: classification runs in parallel across worker threads.
`--max-workers N` controls fanout (default 4, safe for Codex CLI
on a 2-CPU host). Use `--max-workers 1` for deterministic order.

Usage:
    python -m lavandula.reports.tools.classify_null \\
        --db /tmp/0004-coastal-run/data/reports.db \\
        --max-workers 4

Idempotent — re-runs only touch rows still NULL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import signal
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from lavandula.reports.classifier_clients import (
    CodexSubscriptionClient,
    select_classifier_client,
)
from lavandula.reports.classify import (
    ClassifierError,
    classify_first_page,
)


# --- Subprocess tracking (TICK-002 Ctrl-C cleanup) ---------------------
#
# The default `subprocess.run` blocks the worker thread for up to 60s
# on each `codex exec`. `ThreadPoolExecutor.shutdown(cancel_futures=True)`
# only stops *pending* futures — it cannot interrupt an in-flight
# subprocess. Without explicit kill, Ctrl-C leaves up to N orphan
# codex processes running until they hit the 60s timeout.
#
# We inject a tracking runner into `CodexSubscriptionClient` that
# records each active Popen. On Ctrl-C, `kill_active_subprocesses()`
# terminates them so shutdown is prompt.

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
    """Terminate any tracked in-flight Codex subprocesses. Returns count."""
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
    """Return a truthful classifier_model string for the audit trail."""
    if isinstance(client, CodexSubscriptionClient):
        return f"codex-cli/{client._codex_model or 'default'}"
    return result.classifier_model


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# Per-thread classifier client (TICK-002 round-3 review).
#
# The Anthropic SDK keeps an httpx.Client; the Codex shim builds
# per-call subprocesses but holds config state. Neither is audited
# as thread-safe for shared use across workers. Match the crawler's
# per-thread pattern: each worker thread lazily constructs its own
# client via threading.local.

_classifier_local = threading.local()
_classifier_factory = None  # set by main() before dispatch


def _get_thread_classifier():
    c = getattr(_classifier_local, "client", None)
    if c is None:
        c = _classifier_factory()
        # Inject the tracking runner so Ctrl-C can kill in-flight codex
        # subprocesses started by this thread's client.
        if isinstance(c, CodexSubscriptionClient):
            c._runner = _tracking_subprocess_run
        _classifier_local.client = c
    return c


def _classify_one(sha: str, text: str):
    """Worker-side classify call. Returns (sha, result_or_exc_tuple)."""
    client = _get_thread_classifier()
    try:
        result = classify_first_page(
            text, client=client, raise_on_error=False
        )
        return sha, ("ok", result)
    except ClassifierError as exc:
        return sha, ("schema_error", exc)
    except Exception as exc:  # noqa: BLE001
        return sha, ("unexpected", exc)


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
    ap.add_argument("--max-workers", type=int, default=4,
                    help="Parallel classifier threads (TICK-002). Default 4. "
                    "Codex CLI fanout on a 2-CPU host; lower to 2 if "
                    "rate-limiting is observed. Use 1 for serial.")
    args = ap.parse_args()

    if args.max_workers < 1 or args.max_workers > 32:
        ap.error("--max-workers must be between 1 and 32")

    if not args.db.exists():
        print(f"error: {args.db} not found", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

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
    print(f"classifying {total} rows from {args.db} (max_workers={args.max_workers})")
    if total == 0:
        return 0

    # Per-thread clients (round-3): workers lazily construct their own
    # via threading.local. We need a sample client up front only to
    # compute the effective_classifier_model for audit stamps and to
    # print the class name. The sample is NOT used by workers.
    global _classifier_factory
    _classifier_factory = select_classifier_client
    # Clear main-thread-cached client from any prior main() invocation
    # (test isolation).
    if hasattr(_classifier_local, "client"):
        del _classifier_local.client
    sample_client = _get_thread_classifier()
    print(f"client: {type(sample_client).__name__}\n")

    ok = 0
    errs = 0
    unknown_enum = 0
    low_confidence = 0
    classification_counts: dict[str, int] = {}
    # DB writes serialized via a lock — single connection, multi-thread callers.
    db_lock = threading.Lock()

    def _write_result(sha: str, result) -> None:
        with db_lock:
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
                    _effective_classifier_model(sample_client, result),
                    1,
                    iso_now(),
                    sha,
                ),
            )
            conn.commit()

    executor = ThreadPoolExecutor(
        max_workers=args.max_workers,
        thread_name_prefix="classify-null",
    )
    interrupted = False
    try:
        futures = {
            executor.submit(_classify_one, row["content_sha256"], row["first_page_text"]): i
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

            if kind == "schema_error":
                unknown_enum += 1
                print(f"  [{i:>3}/{total}] sha={sha[:10]}  SCHEMA ERROR: {payload}")
                continue
            if kind == "unexpected":
                errs += 1
                print(f"  [{i:>3}/{total}] sha={sha[:10]}  UNEXPECTED: "
                      f"{type(payload).__name__}: {payload}")
                continue

            result = payload
            if result.classification is None:
                errs += 1
                print(f"  [{i:>3}/{total}] sha={sha[:10]}  NULL (error: {result.error})")
                continue

            classification_counts[result.classification] = (
                classification_counts.get(result.classification, 0) + 1
            )
            if (result.classification_confidence is not None
                    and result.classification_confidence < 0.8):
                low_confidence += 1

            _write_result(sha, result)
            ok += 1
            print(f"  [{i:>3}/{total}] sha={sha[:10]}  "
                  f"{result.classification:<12} "
                  f"conf={result.classification_confidence:.2f}")
    except KeyboardInterrupt:
        # Interrupt path: cancel pending futures AND kill any in-flight
        # codex subprocesses. Do NOT wait for them — they're gone.
        interrupted = True
        print("\n^C — cancelling pending classifications", file=sys.stderr)
        executor.shutdown(wait=False, cancel_futures=True)
        killed = kill_active_subprocesses()
        if killed:
            print(f"killed {killed} in-flight codex subprocess(es)",
                  file=sys.stderr)
        raise
    else:
        # Normal completion: drain the (already-drained) pool cleanly.
        executor.shutdown(wait=True)

    # Backfill crawled_orgs.confirmed_report_count from the now-classified
    # reports table. TICK-002 deferred classification out of the crawler
    # so the column is written as 0 at crawl time; this restores the
    # invariant that `confirmed_report_count` reflects the current
    # annual/impact/hybrid count per EIN.
    try:
        with db_lock:
            conn.execute(
                """
                UPDATE crawled_orgs
                   SET confirmed_report_count = (
                     SELECT COUNT(*) FROM reports
                      WHERE reports.source_org_ein = crawled_orgs.ein
                        AND reports.classification IN ('annual', 'impact', 'hybrid')
                   )
                """
            )
            conn.commit()
        print(f"\nbackfilled crawled_orgs.confirmed_report_count")
    except sqlite3.Error as exc:
        print(f"\nwarning: confirmed_report_count backfill failed: {exc}",
              file=sys.stderr)

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

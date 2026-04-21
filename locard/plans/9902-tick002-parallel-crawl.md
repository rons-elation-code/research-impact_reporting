# Plan 9902 — TICK-002: Parallelize crawl + defer classification

**Spec**: `locard/specs/9902-tick002-parallel-crawl-focused.md` (focused)
             `locard/specs/0004-site-crawl-report-catalogue.md` (canonical)
**Date**: 2026-04-21

---

## Overview

Three changes to the crawler + classifier pipeline:
1. Parallelize per-org crawl loop with 8 workers
2. Remove inline classification from crawler (defer to `classify_null.py`)
3. Parallelize `classify_null.py` with 4 workers

Concurrency model:
- **SQLite writes**: single-writer queue (`queue.Queue(maxsize=256)`)
- **HTTP client**: per-thread `ReportsHTTPClient`
- **Per-host throttle**: module-level `HostThrottle` singleton with reservation pattern
- **Memory cap**: 50 MB streaming cap per PDF download

---

## Files to read first

1. `lavandula/reports/crawler.py` — `run()` and `process_org()` functions
2. `lavandula/reports/http_client.py` — `ReportsHTTPClient.tick_throttle()` and throttle state
3. `lavandula/reports/fetch_pdf.py` — `download()` function (needs streaming + size cap)
4. `lavandula/reports/db_writer.py` — all writers that will route through the queue
5. `lavandula/reports/tools/classify_null.py` — main loop
6. `lavandula/reports/tests/unit/test_crawler.py` — existing test patterns

---

## Step 1 — New file: `lavandula/reports/host_throttle.py`

```python
class HostThrottle:
    """Module-level singleton for cross-thread politeness."""
    def __init__(self, min_interval_sec: float = 1.0): ...
    def reserve(self, host: str) -> float:
        """Claim the next slot; return sleep duration.
        Updates last_fetch_time BEFORE returning so concurrent
        callers compute correct delays."""
        with self._lock:
            now = time.monotonic()
            next_allowed = self._last_fetch.get(host, 0.0) + self._min_interval
            wait = max(0.0, next_allowed - now)
            self._last_fetch[host] = now + wait
            return wait

_SINGLETON = HostThrottle()

def reserve(host: str) -> float:
    return _SINGLETON.reserve(host)
```

---

## Step 2 — Modify `lavandula/reports/http_client.py`

`ReportsHTTPClient.tick_throttle(host)` delegates to
`host_throttle.reserve(host)` and `time.sleep(wait)` outside any lock.

Remove the internal `host → last_fetch_time` dict from the instance;
throttle state is now global.

Remove any instance-level lock — the singleton handles it.

---

## Step 3 — Modify `lavandula/reports/fetch_pdf.py`

In `download()`:
- Change `client.get(url, kind="pdf-get")` to use `stream=True`
- Read response in 64 KB chunks, accumulating into a buffer
- If buffer exceeds 50 MB, abort: log structured error, return a
  failure outcome
- Otherwise proceed as before (structure validation, archive, hash)

Expose `MAX_PDF_BYTES = 50 * 1024 * 1024` as a module constant.

---

## Step 4 — New file: `lavandula/reports/db_queue.py`

The queue carries **callables**, not raw SQL. `upsert_report` and
`upsert_crawled_org` do read-then-write logic that can't be reduced to a
single statement, so workers submit a callable that takes a `conn`
argument and performs the full logic on the writer thread.

```python
from queue import Queue, Empty
from threading import Thread, Event
from typing import Callable
import sqlite3

WriteOp = Callable[[sqlite3.Connection], None]

class DBWriter:
    """Single-thread SQLite writer fed by a bounded Queue of callables."""
    def __init__(self, db_path: str, maxsize: int = 256): ...
    def start(self) -> None: ...
    def put(self, op: WriteOp, timeout: float = 30.0) -> None: ...
    def stop(self) -> None:
        """Flush queue and join writer thread; re-raise any exception."""
    def is_alive(self) -> bool: ...
```

Internal loop:
```python
self._conn = sqlite3.connect(self.db_path)
while not self._stop.is_set() or not self._q.empty():
    try:
        op = self._q.get(timeout=0.5)
    except Empty:
        continue
    try:
        op(self._conn)       # existing db_writer functions use conn.execute
        self._conn.commit()  # one commit per op — matches current behavior
    except Exception as exc:
        self._exc = exc
        self._stop.set()
        break
```

On `stop()`, join the thread and re-raise `self._exc` if set.

---

## Step 5 — Modify `lavandula/reports/db_writer.py`

Every public write function (`record_fetch`, `upsert_crawled_org`,
`upsert_report`, `record_deletion`) grows an optional `db_writer` kwarg.

**Preserve the existing function body** — don't rewrite the SQL logic.
Just wrap the whole body as a closure that runs against a passed-in
`conn`, and if `db_writer` is provided, submit the closure to the queue.

Example for `upsert_report`:
```python
def upsert_report(conn, *, db_writer=None, **kwargs):
    def _do(target_conn):
        existing = target_conn.execute(...)  # unchanged logic
        ...
        target_conn.execute(INSERT_OR_UPDATE_SQL, ...)
    if db_writer is not None:
        db_writer.put(_do)
    else:
        _do(conn)
        conn.commit()
```

All existing multi-statement semantics (SELECT-then-INSERT, row
counting, attribution ranking) stay on the writer thread intact.
`lastrowid` and return values remain `None` — no current caller relies
on them, but verify per-function before touching.

---

## Step 6 — Modify `lavandula/reports/crawler.py`

Remove the inline classification call in `process_org()`. Rows are
inserted with `classification=NULL`.

Replace the `for seed in seeds:` loop in `run()` with:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
from .db_queue import DBWriter

writer = DBWriter(db_path=...)
writer.start()

try:
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {
            pool.submit(process_org, seed, ..., db_writer=writer): seed
            for seed in seeds
        }
        for fut in as_completed(futures):
            seed = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                log.error("ein=%s failed: %s", seed.ein, exc)
            # Monitor writer health
            if not writer.is_alive():
                raise RuntimeError("DB writer died; aborting")
finally:
    writer.stop()
```

Add CLI arg `--max-workers N` (default 8, min 1, max 32).

Each worker creates its own `ReportsHTTPClient` inside `process_org`.

---

## Step 7 — Modify `lavandula/reports/tools/classify_null.py`

Replace serial `for row in rows:` with `ThreadPoolExecutor(max_workers=args.max_workers)`.
Default `--max-workers 4`.

Each classify call uses `subprocess.run(timeout=60)` (applies only to
Codex CLI backend; Anthropic uses HTTP timeout).

On `KeyboardInterrupt`, `executor.shutdown(wait=False, cancel_futures=True)`
and kill any still-running Codex subprocesses.

---

## Step 8 — Tests

New files:

**`lavandula/reports/tests/unit/test_crawler_parallel_tick002.py`**:
- `test_ac1_crawler_uses_thread_pool`
- `test_ac3_one_org_failure_does_not_abort_run`
- `test_ac6_host_throttle_serializes_same_host_concurrent_calls`
- `test_ac7_max_workers_1_preserves_serial_behavior`
- `test_writer_death_aborts_run`

**`lavandula/reports/tests/unit/test_host_throttle_tick002.py`**:
- `test_reservation_updates_last_fetch_before_returning`
- `test_concurrent_reserves_same_host_compute_sequential_delays`
- `test_different_hosts_do_not_block_each_other`

**`lavandula/reports/tests/unit/test_db_queue_tick002.py`**:
- `test_put_writes_are_serialized`
- `test_writer_exception_is_reraised_on_stop`
- `test_bounded_queue_applies_backpressure`

**`lavandula/reports/tests/unit/test_fetch_pdf_size_cap_tick002.py`**:
- `test_download_aborts_past_50mb`
- `test_download_succeeds_under_50mb`
- `test_streaming_does_not_buffer_all_at_once`

**`lavandula/reports/tests/unit/test_classify_null_parallel_tick002.py`**:
- `test_ac4_classify_null_uses_thread_pool`
- `test_classify_subprocess_timeout_enforced`
- `test_keyboard_interrupt_kills_subprocesses`

---

## Step 9 — Integration sanity check

After unit tests pass, builder should NOT run live crawl. The architect
runs the end-to-end validation against `seeds-haiku.db` post-merge.

---

## Acceptance Criteria Checklist

- [ ] AC1 — `crawler.run()` uses `ThreadPoolExecutor` with `--max-workers`
- [ ] AC2 — `process_org()` writes rows with `classification=NULL`
- [ ] AC3 — Single-org failures don't abort the run
- [ ] AC4 — `classify_null.py` uses `ThreadPoolExecutor(--max-workers)`
- [ ] AC5 — Deferred to architect post-merge validation
- [ ] AC6 — Per-host throttle serializes concurrent same-host requests
- [ ] AC7 — `--max-workers=1` produces identical results to pre-TICK-002
- [ ] Writer death aborts the run
- [ ] 50 MB PDF cap enforced via streaming
- [ ] Subprocess cleanup on classify_null shutdown

---

## Traps to Avoid

1. **Don't share `ReportsHTTPClient` across threads** — per-thread instance
2. **Don't direct-write to SQLite from workers** — route through `DBWriter`
3. **Don't compute throttle delay outside the singleton lock** — use `reserve()`
4. **Don't buffer full PDF response in memory** — stream with size check
5. **Don't swallow writer exceptions** — re-raise on `stop()`
6. **Don't leave orphaned Codex subprocesses** — kill on shutdown

## TICK-002 — Parallelize crawl + defer classification (2026-04-21)

### Why

Sequential processing of the per-org crawl loop, plus inline PDF classification
via the Codex CLI, creates a wall-clock cost roughly 10x what the actual work
requires. Observed on the TX 88-org Haiku run: ~3-4 hours for work that is
~15-25 minutes of real compute/network time. Each org uses a different host, so
the per-host HTTP throttle does not justify serial org processing.

### Design

**Change 1 — Parallelize the per-org crawl loop.**

Replace the sequential `for seed in seeds:` loop in `crawler.run()` with a
`concurrent.futures.ThreadPoolExecutor(max_workers=8)`. Each worker thread
calls `process_org()` end-to-end for one seed. The per-host throttle in
`ReportsHTTPClient` already serializes requests to the same host, and the
`nonprofits_seed.website_url` column gives distinct hosts per org, so 8 workers
produce ~8x throughput without hammering any single site.

The default `max_workers=8` is configurable via a new `--max-workers N` CLI
flag (default 8, min 1, max 32). `max_workers=1` preserves today's behavior
for debugging.

**Change 2 — Defer classification out of the crawler.**

Strip the inline `classify_first_page()` call from `process_org()`. The
crawler's responsibility is now: discover → fetch → extract first-page text →
write rows with `classification IS NULL`.

A separate post-crawl step uses the existing `classify_null.py` tool.
Operators run it after the crawler completes, or run both back-to-back
in a wrapper script. No change to the classifier interface.

**Change 3 — Parallelize classification.**

`classify_null.py` today iterates rows serially. Replace with a
`ThreadPoolExecutor(max_workers=4)`. Each worker calls the classifier client
(Codex CLI or Anthropic) for one row. `max_workers=4` is a safe default for
Codex CLI subprocess fanout on a 2-CPU host; configurable via
`--max-workers N`.

### Acceptance Criteria

**AC1** — `crawler.run()` processes orgs via `ThreadPoolExecutor` with
configurable `--max-workers` (default 8).

**AC2** — `process_org()` no longer calls `classify_first_page()`. Rows
are written with `classification=NULL`.

**AC3** — `crawler.run()` exits with normal status after all orgs finish,
even with worker failures on individual orgs. Errors on one org do not
abort the run; they are logged and recorded in `fetch_log`.

**AC4** — `classify_null.py` accepts `--max-workers N` (default 4) and
processes rows in parallel.

**AC5** — On the TX 88-org seeds-haiku.db dataset, end-to-end
crawl + classify wall time drops from 3-4 hrs to under 30 min.

**AC6** — Per-host HTTP throttle still serializes correctly. Verified by
unit test: two threads requesting the same host observe the QPS tick.

**AC7** — `--max-workers=1` preserves deterministic serial behavior for
debugging.

### Files Changed

| File | Change |
|------|--------|
| `lavandula/reports/crawler.py` | `run()` uses `ThreadPoolExecutor`; remove inline classify call in `process_org()` |
| `lavandula/reports/tools/classify_null.py` | `main()` uses `ThreadPoolExecutor` |
| `lavandula/reports/tests/unit/test_crawler_parallel_tick002.py` | NEW — AC1, AC3, AC6, AC7 |
| `lavandula/reports/tests/unit/test_classify_null_parallel_tick002.py` | NEW — AC4 |

### Traps to avoid

1. **SQLite writes from multiple threads.** `sqlite3.connect()` is not
   thread-safe by default. Each worker thread must use its own connection,
   OR writes must be serialized via a single writer thread with a queue.
   Favor option 2 (single writer) — simpler, avoids lock contention.

2. **Per-host throttle correctness.** `ReportsHTTPClient.tick_throttle()`
   must remain thread-safe; verify the internal `host → last_fetch_time`
   dict is protected by a lock. If not, add one.

3. **Classifier API fanout.** 4 concurrent `codex exec` subprocesses on a
   2-CPU host is near the edge. If Codex CLI starts rate-limiting or
   timing out, reduce default to 2.

4. **Deterministic output order.** Parallel execution scrambles log ordering.
   Do not rely on sequential order in tests; match on contents only.

5. **Backward compatibility.** `--max-workers=1` must produce identical
   results to the pre-TICK-002 behavior for any given seed set.

### Concurrency architecture (added after multi-agent review)

**SQLite writes — pinned to single-writer queue.** Worker threads
never write to SQLite directly. They `put()` `WriteOp` records (namedtuple
of SQL + params) onto a `queue.Queue`. A single dedicated writer thread
consumes the queue and executes writes on one connection. This avoids
all lock contention and makes reasoning about transaction boundaries
trivial. The queue is bounded (`maxsize=256`) to apply backpressure.

**HTTP client — per-thread instance.** Each worker constructs its own
`ReportsHTTPClient` inside its thread. `requests.Session` is not
thread-safe (shared cookies, connection pool state), so sharing one
client across workers would introduce race conditions. Per-thread
clients cost a small amount of memory and are the simplest fix.

**Per-host throttle — module-level shared dict + lock.** The throttle
state (`host → last_fetch_time`) must be shared across per-thread
clients so they coordinate politeness. Move this state into a
module-level singleton `HostThrottle` protected by a `threading.Lock`.
Each per-thread `ReportsHTTPClient` delegates throttle checks to
the singleton.

### Updated traps (supersedes earlier draft)

1. ~~SQLite writes from multiple threads.~~ **Resolved**: single-writer
   queue. Workers never `execute()` writes directly.

2. **`requests.Session` thread safety**: per-thread clients. Do not share
   a single `ReportsHTTPClient` across workers.

3. **Host throttle must remain global**: move `last_fetch_time` state
   to `HostThrottle` singleton. Workers share the singleton, not the
   HTTP client.

4. Classifier API fanout: 4 concurrent Codex calls on a 2-CPU host is
   the ceiling. Start at default 4, back off to 2 on first observed
   rate-limit error.

5. Deterministic output order: do not rely on sequential log order in
   tests; match on contents.

6. Backward compatibility: `--max-workers=1` must produce identical
   results to pre-TICK-002 for any seed set. Add a regression test.

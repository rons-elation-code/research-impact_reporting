# Plan 0021 — Async I/O Crawler Pipeline

**Spec**: `locard/specs/0021-async-crawler-pipeline.md`
**Created**: 2026-04-25

---

## Implementation Order

8 phases, bottom-up. Each phase is independently testable. Phases 1-3 are foundational; 4-6 build the pipeline stages; 7 wires everything together; 8 adds observability.

### Phase 1: Async Host Throttle (`async_host_throttle.py`)

**New file**: `lavandula/reports/async_host_throttle.py`

**What to build:**
- `AsyncHostThrottle` class with `async wait(host)` and `release(host)` methods
- Per-host `asyncio.Semaphore(1)` created lazily (on first access, under `asyncio.Lock`)
- Per-host timestamp dict tracking last request time
- `wait()`: acquire semaphore → compute delay since last request → `await asyncio.sleep(max(0, delay))` → update timestamp
- `release()`: release the semaphore
- Constructor takes `min_interval_sec` (default 3.0) and `jitter_sec` (default 0.5)
- `reset()` method for testing

**ACs covered**: AC7

**Tests** (new file `lavandula/reports/tests/unit/test_async_host_throttle.py`):
- Mock `loop.time()` to verify >= 3s gap between requests to same host
- Concurrent requests to different hosts proceed without waiting
- Concurrent requests to same host serialize
- Jitter is applied within bounds

**Lines**: ~80

---

### Phase 2: Async DNS Pin Cache (`async_host_pin_cache.py`)

**New file**: `lavandula/reports/async_host_pin_cache.py`

**What to build:**
- `AsyncHostPinCache` implementing `aiohttp.abc.AbstractResolver`
- `resolve(host, port, family)` method:
  1. Check cache (positive and negative)
  2. If miss: `await loop.run_in_executor(None, socket.getaddrinfo, host, port, AF_INET, SOCK_STREAM)` (prefer IPv4)
  3. If no IPv4: try IPv6
  4. Pick first result; call `is_address_allowed(ip)`
  5. If disallowed: cache negative result, raise `aiohttp.ClientConnectorError`
  6. Cache positive result
  7. Return `[{"hostname": host, "host": ip, "port": port, "family": family, "proto": 0, "flags": 0}]`
     - **Critical**: `hostname` field = original host (for TLS SNI/cert validation)
- `close()` method (no-op, required by interface)
- Thread-safe: NO — single event loop only

**ACs covered**: AC10, AC11, AC12, AC13, AC14

**Tests** (new file `lavandula/reports/tests/unit/test_async_host_pin_cache.py`):
- Mock `getaddrinfo`: positive pin (public IP) → cached on second call
- Mock `getaddrinfo`: private IP → `ClientConnectorError`, negative cached
- IPv4 preferred over IPv6 when both available
- `hostname` field preserved in result dict
- Each unique host triggers independent resolution
- TLS hostname verification test: use `aiohttp.TCPConnector(resolver=cache)` against a local stub HTTPS server with a cert for `wrong.example` — verify connection fails

**Lines**: ~100

---

### Phase 3: Async HTTP Client (`async_http_client.py`)

**New file**: `lavandula/reports/async_http_client.py`

**What to build:**
- `AsyncHTTPClient` class as async context manager
- `__aenter__`: create `aiohttp.ClientSession` with:
  - `TCPConnector(limit=500, limit_per_host=2, use_dns_cache=False, resolver=self._pin_cache, keepalive_timeout=60)`
  - `auto_decompress=False`
  - `timeout=ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=15)`
  - Default headers: User-Agent, Accept-Encoding (gzip, identity), Accept
  - Cookie jar: `aiohttp.DummyCookieJar()` (don't persist cookies across requests)
- `__aexit__`: close session
- `_check_open()`: raise `RuntimeError` if session not created

**`get(url, *, kind, seed_etld1)` → `FetchResult`:**
1. Normalize protocol-relative URLs (`//` → `https://`)
2. Validate scheme (http only if `allow_insecure_cleartext`)
3. Manual redirect loop (max `config.MAX_REDIRECTS`):
   a. `await self._throttle.wait(host)`
   b. `await session.get(url, allow_redirects=False, headers={"Referer": ""})`
   c. `self._throttle.release(host)`
   d. If 3xx: `check_redirect_chain(chain, seed_etld1)` → follow or reject
   e. If 200: `_decompress_stream(resp)` — read chunks with `resp.content.read(8192)`, manual zlib for gzip
   f. Content-Encoding check: reject anything not `gzip`/`identity`/absent → `blocked_content_type`
4. Build and return `FetchResult` (same dataclass as sync client)
5. Retry logic: `RETRY_STATUSES`, `RETRY_KINDS`, `RETRY_MAX_ATTEMPTS`, `RETRY_BACKOFF_SEC`

**`head(url, *, kind)` → `FetchResult`:**
- Similar but simpler (no body, no decompression)

**Key imports from existing code** (reuse, don't duplicate):
- `FetchResult` from `http_client.py` (same dataclass)
- `check_redirect_chain` from `redirect_policy.py`
- `redact_url` from `url_redact.py`
- `config.*` for caps, timeouts, headers

**ACs covered**: AC1-AC6, AC8, AC9, AC40

**Tests** (new file `lavandula/reports/tests/unit/test_async_http_client.py`):
- Parity test: stub server returns same response → `FetchResult` fields identical to sync client
- `auto_decompress=False` verified (gzip body decompressed manually)
- Content-Encoding `br` → `blocked_content_type`
- Content-Encoding `deflate` → `blocked_content_type`
- Decompressed-byte cap fires on gzip bomb
- Redirect gating: cross-origin redirect blocked
- Protocol-relative URL normalized
- Retry: network error on first try, success on second
- `get()` outside context manager → `RuntimeError`
- Timeout: slow response → appropriate error

**Lines**: ~350

---

### Phase 4: Async Discovery (`async_discover.py`)

**New file**: `lavandula/reports/async_discover.py`

**What to build:**
- `async def discover_org(seed_url, seed_etld1, client, robots_text, ein, db_writer_queue)` → `list[Candidate]`
- Reimplements the orchestration logic of `discover.per_org_candidates` using `await client.get()` calls
- Reuses all pure functions from existing modules:
  - `extract_candidates`, `classify_sitemap_url`, `_anchor_matches`, `_path_matches` from `candidate_filter.py`
  - `robots_can_fetch`, `sitemap_urls_from_robots` from `robots.py`
  - `parse_sitemap_index_recursive`, `parse_sitemap` from `sitemap.py`
  - `canonicalize_url` from `url_redact.py`
  - `etld1` from `redirect_policy.py`
- Same logic: robots → sitemap → homepage → subpage expansion → dedup → cap
- Same TICK-001/TICK-004/TICK-007 behavior
- `record_fetch` calls enqueued to `db_writer_queue` instead of called directly

**ACs covered**: AC26 (parity), AC28, AC29

**Tests** (new file `lavandula/reports/tests/unit/test_async_discover.py`):
- Parity test: same canned HTML/robots → same candidate set as sync `per_org_candidates`
- Report-anchor subpage relaxation works identically
- Subpage priority sorting preserved

**Lines**: ~200

---

### Phase 5: Async PDF Download (`async_fetch_pdf.py`)

**New file**: `lavandula/reports/async_fetch_pdf.py`

**What to build:**
- `async def download(url, client, *, seed_etld1, validate_structure, process_pool)` → `DownloadOutcome`
- Same logic as `fetch_pdf.download`:
  1. `await client.head(url)` — skip if non-PDF Content-Type
  2. `await client.get(url, kind="pdf-get")` — fetch body
  3. `is_pdf_magic(body[:32])` check
  4. If `validate_structure`: `await loop.run_in_executor(process_pool, _validate_pdf_structure_inner, body)` with `Future.result(timeout=30)`
  5. On timeout: terminate worker, return `pdf_structure_timeout`
  6. SHA256 hash, return `DownloadOutcome`

**Reuses**:
- `is_pdf_magic` from `fetch_pdf.py`
- `_validate_pdf_structure_inner` from `fetch_pdf.py`
- `DownloadOutcome` from `fetch_pdf.py`

**ACs covered**: AC20, AC21

**Tests** (new file `lavandula/reports/tests/unit/test_async_fetch_pdf.py`):
- HEAD skip on non-PDF Content-Type
- Magic byte check
- Structure validation in ProcessPool (verify PID changes between calls = `max_tasks_per_child=1`)
- Validation timeout (mock slow validator)

**Lines**: ~120

---

### Phase 6: DB Writer Actor (`async_db_writer.py`)

**New file**: `lavandula/reports/async_db_writer.py`

**What to build:**

**Typed write requests** (discriminated union):
```python
@dataclass
class RecordFetchRequest:
    op: str = "record_fetch"
    ein: str; url_redacted: str; kind: str; fetch_status: str
    status_code: int | None; elapsed_ms: int | None; notes: str | None

@dataclass
class UpsertReportRequest:
    op: str = "upsert_report"
    # all fields from db_writer.upsert_report

@dataclass
class UpsertCrawledOrgRequest:
    op: str = "upsert_crawled_org"
    ein: str; candidate_count: int; fetched_count: int; confirmed_report_count: int
    status: str = "success"  # or "permanent_skip"
```

**`OrgDownloadTracker`:**
```python
class OrgDownloadTracker:
    def __init__(self): self._pending = 0; self._done = asyncio.Event(); self._done.set()
    def increment(self): self._pending += 1; self._done.clear()
    def decrement(self): self._pending -= 1; if self._pending == 0: self._done.set()
    async def wait_all_done(self): await self._done.wait()
```

**`DBWriterActor`:**
- Constructor: `engine`, `max_queue=200`, `batch_size=50`, `flush_interval_sec=5.0`
- Creates `asyncio.Queue(maxsize=max_queue)` and `ThreadPoolExecutor(max_workers=1)`
- `async enqueue(request)`: put on queue (blocks if full = backpressure)
- `async run()`: main loop
  - `asyncio.wait_for(queue.get(), timeout=flush_interval_sec)` to implement timer
  - Accumulate batch; when `batch_size` reached or timer fires → flush
  - Flush: group by op type, call `db_writer.record_fetch` / `upsert_report` / `upsert_crawled_org` via `run_in_executor`
  - On flush failure: log, retry once, then log with full payload for manual recovery
- `async flush_and_stop()`: drain queue, flush remaining, shut down executor
  - Shielded from cancellation: wrapped in `asyncio.shield()` at call site

**ACs covered**: AC18, AC19, AC22, AC39

**Tests** (new file `lavandula/reports/tests/unit/test_async_db_writer.py`):
- Batch accumulation: 50 requests → one flush
- Timer flush: 3 requests + 5s timeout → flush
- Backpressure: queue full → enqueue blocks
- Retry on flush failure
- `flush_and_stop`: all remaining items flushed
- `OrgDownloadTracker`: barrier fires correctly

**Lines**: ~180

---

### Phase 7: Async Crawler Orchestrator (`async_crawler.py`)

**New file**: `lavandula/reports/async_crawler.py`

**What to build:**

**`async def run_async(engine, archive, seeds, *, max_concurrent_orgs, max_download_workers, run_id, halt_dir)`** → `CrawlStats`

1. **Setup**:
   - Create `AsyncHostThrottle`, `AsyncHostPinCache`, `AsyncHTTPClient` (as context manager)
   - Create `ProcessPoolExecutor(max_workers=4, max_tasks_per_child=1)`
   - Create `DBWriterActor(engine)` — start as background task
   - Create `asyncio.Queue(maxsize=1000)` for download queue
   - Create `asyncio.Queue(maxsize=max_concurrent_orgs)` for org queue
   - Create `asyncio.Event()` for shutdown
   - Validate halt-dir permissions (not world-writable)

2. **Signal handlers**:
   - First SIGINT/SIGTERM: set `shutdown_event`
   - Second SIGINT within 5s: `os._exit(1)`

3. **Start coroutines**:
   - `org_producer(seeds, org_queue, shutdown_event)` — 1 task
   - `org_worker(...)` x `max_concurrent_orgs` — N tasks
   - `download_worker(...)` x `max_download_workers` — M tasks
   - `halt_sentinel(halt_dir, shutdown_event)` — 1 task
   - `progress_reporter(stats, shutdown_event)` — 1 task
   - `db_actor.run()` — 1 task

4. **Main wait**: `await asyncio.gather(producer_task, *org_worker_tasks)`
   - When producer + all org workers done → all discovery is complete
   - Send poison pills to download workers (None × M)
   - `await asyncio.gather(*download_worker_tasks)`
   - `await db_actor.flush_and_stop()` (shielded)
   - Cancel sentinel + reporter

5. **Shutdown path** (when `shutdown_event` is set):
   - Producer stops feeding
   - Org workers finish current org (barrier ensures downloads complete)
   - Remaining download queue items processed
   - DB actor flushed

6. **Cleanup**: close HTTP client, shutdown process pool, dispose engine

**CLI integration** (modify `lavandula/reports/crawler.py`):
- Add `--async` flag to argparse
- When `--async`: validate incompatibility with `--max-workers`, add `--max-concurrent-orgs` and `--max-download-workers` args
- Call `asyncio.run(run_async(...))` instead of the synchronous loop
- Same flock, encryption check, TLS self-test run BEFORE `asyncio.run`

**`_is_transient(exc)` classifier:**
- `aiohttp.ClientConnectorError`, `aiohttp.ServerTimeoutError`, `asyncio.TimeoutError`, `ConnectionError`, `OSError` → transient
- Everything else → permanent

**ACs covered**: AC15, AC16, AC17, AC22-AC25, AC27, AC30-AC35, AC38, AC41

**Tests** (new file `lavandula/reports/tests/unit/test_async_crawler.py`):
- Parity test (AC26): deterministic stub → same candidates + SHA256s as sync
- Shutdown: SIGINT → DB flushed, no partial crawled_orgs
- Halt-file → same
- Double-SIGINT → force exit
- Resume: interrupted org retried, completed org skipped
- Transient failure: org not marked complete
- Permanent failure: org marked with permanent_skip

**Lines**: ~400

---

### Phase 8: Progress Reporting

**Added to `async_crawler.py`** (not a separate file):

**`CrawlStats` dataclass:**
```python
@dataclass
class CrawlStats:
    orgs_total: int = 0
    orgs_completed: int = 0
    orgs_active: int = 0
    orgs_transient_failed: int = 0
    candidates_discovered: int = 0
    pdfs_downloaded: int = 0
    download_queue_depth: int = 0
    bytes_downloaded: int = 0
    errors_by_type: dict[str, int] = field(default_factory=dict)
    start_time: float = 0.0
```

**`progress_reporter` coroutine:**
- Every 60s, log one line: `orgs: X/Y (Z%) | active: A | queue: Q | PDFs: P | rate: R orgs/hr | ETA: Td Hh`
- At shutdown: final summary line with totals + peak RSS

**ACs covered**: AC36, AC37, AC43

**Lines**: ~80

---

## File Summary

| File | Action | Lines |
|------|--------|-------|
| `lavandula/reports/async_host_throttle.py` | NEW | ~80 |
| `lavandula/reports/async_host_pin_cache.py` | NEW | ~100 |
| `lavandula/reports/async_http_client.py` | NEW | ~350 |
| `lavandula/reports/async_discover.py` | NEW | ~200 |
| `lavandula/reports/async_fetch_pdf.py` | NEW | ~120 |
| `lavandula/reports/async_db_writer.py` | NEW | ~180 |
| `lavandula/reports/async_crawler.py` | NEW | ~480 |
| `lavandula/reports/crawler.py` | MODIFY | ~30 (CLI flag) |
| `lavandula/reports/tests/unit/test_async_host_throttle.py` | NEW | ~80 |
| `lavandula/reports/tests/unit/test_async_host_pin_cache.py` | NEW | ~120 |
| `lavandula/reports/tests/unit/test_async_http_client.py` | NEW | ~200 |
| `lavandula/reports/tests/unit/test_async_discover.py` | NEW | ~100 |
| `lavandula/reports/tests/unit/test_async_fetch_pdf.py` | NEW | ~80 |
| `lavandula/reports/tests/unit/test_async_db_writer.py` | NEW | ~120 |
| `lavandula/reports/tests/unit/test_async_crawler.py` | NEW | ~200 |
| **Total** | | **~2440** |

## Dependencies to Install

```
aiohttp>=3.9
```

No other new dependencies. `aiodns` is NOT required (D7 uses `run_in_executor` for DNS).

## Validation Checklist (Builder)

Before creating the PR:

- [ ] All 43 ACs verified (unit tests + manual check)
- [ ] All existing tests pass (`pytest lavandula/reports/tests/`)
- [ ] `--async` flag works: `python -m lavandula.reports.crawler --async --ein <test-ein> --archive-dir /tmp/test --skip-tls-self-test --skip-encryption-check`
- [ ] `--async` incompatible with `--max-workers` (parser error)
- [ ] Sync crawler still works: `python -m lavandula.reports.crawler --max-workers 1 --ein <test-ein> --archive-dir /tmp/test --skip-tls-self-test --skip-encryption-check`
- [ ] 100-org benchmark run with `--async --max-concurrent-orgs 200` completes in < 30 min (AC42)
- [ ] Peak RSS < 2 GB on 100-org run (AC43)
- [ ] `lint.sh` clean (if exists) or `ruff check` passes

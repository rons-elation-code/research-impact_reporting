# Spec 0021 — Async I/O Crawler Pipeline

**Status**: Draft
**Author**: Architect
**Created**: 2026-04-25
**Dependencies**: 0004 (site-crawl catalogue), 0020 (data-driven taxonomy)

---

## Problem Statement

The current crawler uses synchronous `requests` + `ThreadPoolExecutor` (8 workers). Each worker blocks on `time.sleep(3)` per-host throttle waits, HTTP I/O, and S3 uploads. During the 100-org test run (2026-04-25), the crawler took ~4 hours — roughly 2.4 minutes per org.

At that rate, a national crawl of 100K+ orgs would take **167 days** on a single machine. Even at 32 threads, the blocking sleep pattern means most threads are idle most of the time. The bottleneck is not CPU or bandwidth — it's wasted wall-clock time in `time.sleep()` calls that could be multiplexed across other orgs.

### Why Threads Don't Scale

With 8 threads and a 3-second per-host delay:
- Each org requires ~5-8 HTTP requests (robots + homepage + 2-5 subpages + 1-3 PDF downloads)
- Each request blocks its thread for 3s throttle + ~1-5s network I/O = ~4-8s total
- An org takes ~30-60s of wall time but only ~5-10s of actual I/O
- The remaining ~25-50s per org is `time.sleep()` — pure waste when other orgs need work

With async I/O, those idle periods are yielded to the event loop, which can service hundreds of concurrent orgs with a single thread.

## Goals

1. **10x throughput improvement**: 100K orgs in < 2 weeks on a single machine (target: ~500 orgs/hour sustained)
2. **Politeness preserved**: Per-host rate limiting stays at 3s minimum gap — we go faster by working on more orgs concurrently, not by hitting any single host harder
3. **Incremental migration**: The async layer wraps the existing pipeline stages; filter logic, taxonomy, candidate extraction, and DB schema are unchanged
4. **Operational safety**: Same flock, resume, encryption-at-rest, and halt-file behaviors as today
5. **Observability**: Real-time progress reporting (orgs/min, active connections, queue depths)

## Non-Goals

- Distributed crawling across multiple machines (future spec)
- Changing the discovery algorithm (subpage walking, candidate filtering)
- Modifying the classifier pipeline
- Changing the database schema
- Horizontal auto-scaling

## Architecture

### Current Architecture (Synchronous)

```
ThreadPoolExecutor(8 workers)
  ├── Thread 1: process_org(ein_1) — blocks on sleep + requests
  ├── Thread 2: process_org(ein_2) — blocks on sleep + requests
  └── ...
  Each thread: robots → homepage → subpages → PDFs → DB writes (all serial per org)
```

### Target Architecture (Async Pipeline)

```
asyncio event loop (1 thread)
  ├── Org Scheduler (feeds orgs into pipeline, respects concurrency cap)
  │     └── Priority queue: high-value orgs first
  │
  ├── Discovery Workers (async coroutines, one per active org)
  │     └── Each: robots → homepage → subpages → candidate list
  │         └── Per-host semaphore: max 1 concurrent request per host, 3s gap
  │         └── Produces: Candidate objects → Download Queue
  │
  ├── Download Workers (pool of N coroutines pulling from queue)
  │     └── HEAD probe → filename score check → GET → validate → archive
  │         └── Per-host semaphore: shared with discovery
  │         └── Produces: archived PDF metadata → DB write batch
  │
  └── DB Writer (batched async, single coroutine)
        └── Collects writes, flushes every 100 rows or 5 seconds
```

### Key Design Decisions

**D1: aiohttp, not httpx.** `aiohttp` is battle-tested for high-concurrency crawlers (100K+ connection reuse), has native streaming decompression, and allows raw socket-level control we need for the decompressed-size cap. `httpx` is simpler but its async backend wraps `anyio` which adds overhead we don't need.

**D2: Per-host async semaphore, not global rate limiter.** Each unique host gets an `asyncio.Semaphore(1)` plus a timestamp tracking the last request. Before each request: acquire semaphore → compute delay since last request → `await asyncio.sleep(delay)` → make request → release semaphore. This gives us the same politeness guarantees as today's `HostThrottle` but without blocking a thread.

**D3: Producer-consumer with bounded queue.** Discovery and downloading are decoupled via an `asyncio.Queue(maxsize=1000)`. Discovery coroutines produce candidates; download coroutines consume them. The bounded queue provides natural backpressure — if downloads fall behind, discovery pauses.

**D4: Org concurrency cap, not global connection cap.** We limit to N concurrent orgs (default: 200) rather than N connections. Each org typically talks to 1-2 hosts, so 200 orgs ≈ 200-400 concurrent host slots. This is more intuitive to tune and naturally respects per-host politeness.

**D5: Synchronous DB writes via `run_in_executor`.** SQLAlchemy's connection pool is thread-safe but not async-native. Rather than introducing `asyncpg` (which would require rewriting all SQL), we wrap `db_writer.*` calls in `loop.run_in_executor(thread_pool, ...)` with a small thread pool (4 threads). This is a proven pattern and avoids touching the DB layer.

**D6: Subprocess PDF validation stays synchronous.** The `_validate_pdf_structure` call already spawns a subprocess. We run it via `loop.run_in_executor` to avoid blocking the event loop. The subprocess pool is bounded to prevent fork bombs (max 4 concurrent validations).

## Technical Implementation

### Phase 1: Async HTTP Client (`async_http_client.py`)

New module wrapping `aiohttp.ClientSession` with the same controls as `ReportsHTTPClient`:

```python
class AsyncHTTPClient:
    async def get(self, url: str, *, kind: str, seed_etld1: str | None) -> FetchResult:
        """Same signature and return type as ReportsHTTPClient.get()."""

    async def head(self, url: str, *, kind: str) -> FetchResult:
        """Same signature and return type as ReportsHTTPClient.head()."""
```

Preserves:
- Accept-Encoding: gzip, identity (AC8 from spec 0004)
- Streaming decompressed-byte cap on every encoding
- Every-hop redirect gating via `check_redirect_chain`
- Referer stripping
- URL redaction on all returned URLs
- Protocol-relative URL normalization (`//` → `https://`)

New:
- Uses `aiohttp.ClientSession` with `TCPConnector(limit=0, limit_per_host=1)`
- `connector.limit=0` means no global cap; per-host is governed by our semaphore
- Connection keepalive and reuse across requests to the same host

### Phase 2: Async Host Throttle (`async_host_throttle.py`)

```python
class AsyncHostThrottle:
    """Per-host rate limiter using asyncio primitives."""

    def __init__(self, min_interval_sec: float = 3.0, jitter_sec: float = 0.5):
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._last_request: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, host: str) -> None:
        """Wait until it's safe to request this host, then claim the slot."""

    def release(self, host: str) -> None:
        """Release the host slot after the request completes."""
```

The semaphore ensures only one request per host is in flight. The timestamp ensures the 3s gap. `asyncio.sleep()` yields to the event loop during the gap — other coroutines run while we wait.

### Phase 3: Async Discovery (`async_discover.py`)

Wraps `per_org_candidates` to use the async HTTP client:

```python
async def discover_org(
    seed_url: str,
    seed_etld1: str,
    client: AsyncHTTPClient,
    robots_text: str,
    ein: str = "",
) -> list[Candidate]:
```

The candidate filtering logic (`extract_candidates`, `classify_sitemap_url`, etc.) is pure computation — no I/O — so it runs synchronously within the coroutine. Only the fetch calls (`client.get()`) are awaited.

### Phase 4: Async Download (`async_fetch_pdf.py`)

```python
async def download(
    url: str,
    client: AsyncHTTPClient,
    *,
    seed_etld1: str | None = None,
    validate_structure: bool = True,
    executor: concurrent.futures.ProcessPoolExecutor | None = None,
) -> DownloadOutcome:
```

PDF structure validation runs in `executor` via `loop.run_in_executor()`.

### Phase 5: Async Crawler Orchestrator (`async_crawler.py`)

The main entry point:

```python
async def run_async(
    engine: Engine,
    archive: Archive,
    seeds: list[tuple[str, str]],  # (ein, website_url)
    *,
    max_concurrent_orgs: int = 200,
    max_download_workers: int = 50,
    run_id: str = "",
) -> CrawlStats:
```

**Org scheduling:**
```python
org_semaphore = asyncio.Semaphore(max_concurrent_orgs)

async def process_org_async(ein, website):
    async with org_semaphore:
        candidates = await discover_org(...)
        for cand in candidates:
            await download_queue.put((ein, cand))

# Launch all orgs as tasks — semaphore throttles concurrency
tasks = [asyncio.create_task(process_org_async(e, w)) for e, w in seeds]
await asyncio.gather(*tasks, return_exceptions=True)
```

**Download consumer:**
```python
async def download_worker(queue, client, engine, archive):
    while True:
        ein, cand = await queue.get()
        outcome = await download(cand.url, client, ...)
        if outcome.status == "ok":
            # Archive + DB write via executor
            await loop.run_in_executor(db_pool, db_writer.upsert_report, engine, ...)
        queue.task_done()
```

### Phase 6: Conditional Fetching (Re-crawl Optimization)

For re-crawls (`--refresh`), store and use HTTP caching headers:

- **ETag**: Store `ETag` from response headers in `fetch_log.notes`. On re-crawl, send `If-None-Match`. 304 → skip download.
- **Last-Modified**: Store `Last-Modified` similarly. On re-crawl, send `If-Modified-Since`. 304 → skip.

New DB column: `lava_impact.reports.last_etag TEXT` and `lava_impact.reports.last_modified TEXT`.

This avoids re-downloading unchanged PDFs on subsequent national crawls.

### Phase 7: Org Prioritization

Score orgs before crawling to maximize value per hour:

```python
def org_priority(ein: str, *, engine: Engine) -> float:
    """Higher = crawl sooner. Factors:
    - Revenue band (larger orgs more likely to publish reports)
    - NTEE category (human services, health, education = higher)
    - Prior crawl success (orgs that previously yielded reports = higher on re-crawl)
    - Website platform (Wix/Squarespace = standard structure = faster)
    - Never-crawled > stale-crawl > recently-crawled
    """
```

Seeds are sorted by priority before feeding into the scheduler. This ensures that even if a crawl is interrupted, the highest-value orgs were processed first.

### Phase 8: Progress Reporting

Real-time stats emitted to the log and optionally to a Unix socket for the dashboard:

```
[12:34:56] orgs: 1,234/100,000 (1.2%) | active: 187 | queue: 423 | PDFs: 892 | rate: 512 orgs/hr | ETA: 8d 3h
```

Metrics:
- `orgs_completed`, `orgs_active`, `orgs_total`
- `candidates_discovered`, `pdfs_downloaded`, `pdfs_confirmed`
- `download_queue_depth`
- `requests_per_second`, `bytes_downloaded`
- `errors_by_type` (network, timeout, blocked, etc.)

## Migration Strategy

The async crawler is a **new module** (`async_crawler.py`) alongside the existing `crawler.py`. Both share:
- `candidate_filter.py` (pure computation, no changes)
- `discover.py` — the async version wraps the same logic with an async fetcher
- `db_writer.py` (called via executor)
- `config.py` (shared constants)
- `fetch_pdf.py` — async version wraps the same validation logic

The CLI gains a `--async` flag (or becomes the default after validation):

```bash
# Synchronous (existing, for debugging / comparison)
python -m lavandula.reports.crawler --max-workers 8 ...

# Async (new, for production)
python -m lavandula.reports.crawler --async --max-concurrent-orgs 200 ...
```

Once validated, async becomes the default and the synchronous path is retained only for `--max-workers 1` serial debugging.

## Acceptance Criteria

### Core Async Infrastructure
- **AC1**: `AsyncHTTPClient.get()` returns `FetchResult` with identical fields to `ReportsHTTPClient.get()` for the same URL.
- **AC2**: `AsyncHTTPClient` enforces the decompressed-byte cap (same streaming logic as AC8 of spec 0004).
- **AC3**: `AsyncHTTPClient` applies every-hop redirect gating via `check_redirect_chain`.
- **AC4**: `AsyncHTTPClient` strips Referer, sets User-Agent and Accept-Encoding per config.
- **AC5**: Per-host async throttle enforces >= 3s gap between requests to the same host, verified by unit test with mock clock.

### Pipeline Architecture
- **AC6**: Discovery and download run as separate coroutine pools connected by a bounded `asyncio.Queue`.
- **AC7**: Org concurrency is capped at `--max-concurrent-orgs` (default 200). Exceeding the cap blocks new org starts until an active org completes.
- **AC8**: Download workers (default 50) pull from the shared queue. Worker count is configurable via `--max-download-workers`.
- **AC9**: DB writes are batched and executed via `run_in_executor` on a thread pool. No async DB driver required.
- **AC10**: PDF structure validation runs via `run_in_executor` on a process pool (max 4 concurrent).

### Correctness
- **AC11**: For a fixed set of seed orgs, the async crawler produces the same set of candidate URLs as the synchronous crawler (order may differ).
- **AC12**: All existing unit tests for `candidate_filter`, `discover`, `fetch_pdf`, `redirect_policy`, and `db_writer` continue to pass without modification.
- **AC13**: The async crawler respects robots.txt identically to the synchronous crawler.
- **AC14**: The async crawler applies the same filename scoring, taxonomy-driven filtering, and TICK-001 relaxation as the synchronous crawler.

### Operational Safety
- **AC15**: Flock (AC19 of spec 0004) prevents concurrent async and synchronous crawler instances.
- **AC16**: Resume semantics (AC20 of spec 0004) work identically — already-crawled EINs are skipped unless `--refresh`.
- **AC17**: Encryption-at-rest check and TLS self-test run before the event loop starts.
- **AC18**: Graceful shutdown on SIGINT/SIGTERM: drain in-flight requests, flush pending DB writes, then exit cleanly. No data loss.

### Conditional Fetching
- **AC19**: On `--refresh` re-crawls, PDFs with stored ETag/Last-Modified values send conditional headers. HTTP 304 responses skip download and archive.
- **AC20**: New columns `last_etag` and `last_modified` added to `lava_impact.reports` via migration.

### Org Prioritization
- **AC21**: Seeds are sorted by a priority score before crawling. The scoring function is configurable but defaults to: revenue band > NTEE category > prior crawl success > never-crawled-first.
- **AC22**: If the crawl is interrupted (SIGINT or `--limit`), the highest-priority orgs were processed first.

### Observability
- **AC23**: Progress stats are logged every 60 seconds: orgs completed/total, active count, queue depth, PDFs found, rate (orgs/hr), estimated time remaining.
- **AC24**: A final summary log line reports total orgs, total PDFs, total bytes, wall-clock time, and effective orgs/hr rate.

### Performance
- **AC25**: On a 100-org test set, the async crawler completes in < 30 minutes (vs ~4 hours synchronous), a minimum 8x improvement.
- **AC26**: Memory usage stays under 2 GB for a 1000-org crawl (no unbounded buffering of PDF bodies).

## Traps to Avoid

1. **Don't bypass per-host throttling for speed.** We go faster by multiplexing across hosts, not by hammering any single host. The 3s gap is a policy commitment.

2. **Don't use `asyncpg` or async SQLAlchemy.** The DB writes are lightweight and infrequent compared to HTTP I/O. `run_in_executor` with a 4-thread pool is sufficient and avoids rewriting the entire DB layer.

3. **Don't load all PDF bodies into memory.** The download workers should archive each PDF before pulling the next candidate from the queue. The queue holds `Candidate` objects (metadata only, ~200 bytes each), not PDF bodies.

4. **Don't forget `aiohttp` session cleanup.** `aiohttp.ClientSession` must be explicitly closed or used as an async context manager. Leaking sessions leaks TCP connections.

5. **Don't assume DNS is fast.** `aiohttp` resolves DNS on the event loop by default, which can block. Use `aiohttp.TCPConnector(use_dns_cache=True)` and consider `aiodns` for truly async resolution.

6. **Don't make the bounded queue too large.** A queue of 1000 candidates × ~200 bytes is fine. But if candidates carried PDF bodies, 1000 × 5 MB = 5 GB. Keep the queue metadata-only.

7. **Don't forget to handle `asyncio.CancelledError`.** On shutdown, pending tasks are cancelled. Each coroutine must handle cancellation gracefully (close HTTP connections, don't leave partial DB writes).

## Security Considerations

- **SSRF protections unchanged.** `check_redirect_chain`, `is_address_allowed`, and the cloud-metadata deny list are called from the async HTTP client identically to the synchronous one.
- **No new network exposure.** The async crawler is a client-only change; no new listening sockets or inbound connections.
- **Connection limits prevent resource exhaustion.** The org concurrency cap (200) and per-host semaphore (1) bound total connections to ~200-400 at peak, well within OS limits.
- **Graceful shutdown prevents data corruption.** In-flight DB writes complete before exit; the flock prevents concurrent instances.

## Testing Strategy

1. **Unit tests**: Async HTTP client with mocked `aiohttp` responses. Async throttle with mock clock. Queue backpressure behavior.
2. **Integration tests**: Full pipeline with stub fetcher (same pattern as existing `per_org_candidates` tests). Verify candidate parity with synchronous crawler.
3. **Performance test**: 100-org set with real network. Compare wall-clock time and output parity against synchronous baseline.
4. **Shutdown test**: Send SIGINT during a 10-org crawl, verify all in-flight work is flushed and no data is lost.

## Estimated Effort

- Phase 1-2 (async HTTP + throttle): 200-300 lines
- Phase 3-4 (async discover + download): 200-300 lines (mostly wrappers)
- Phase 5 (orchestrator): 200-300 lines
- Phase 6 (conditional fetching): 50-100 lines + migration
- Phase 7 (org prioritization): 50-100 lines
- Phase 8 (progress reporting): 50-100 lines
- Tests: 300-500 lines
- **Total**: ~1100-1700 lines of new code, ~0 lines of modified existing code

The existing synchronous modules are left untouched. The async pipeline is additive.

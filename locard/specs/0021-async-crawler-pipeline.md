# Spec 0021 — Async I/O Crawler Pipeline

**Status**: Draft (v2 — post-consultation)
**Author**: Architect
**Created**: 2026-04-25
**Dependencies**: 0004 (site-crawl catalogue), 0020 (data-driven taxonomy)

---

## Consultation Log

| Round | Model | Verdict | Key Issues |
|-------|-------|---------|------------|
| 1 | Claude | REQUEST_CHANGES | DNS pinning under aiohttp unaddressed; scope bundles 3 features; AsyncHTTPClient lifecycle missing; AC11 parity needs stub fixture; missing ACs for halt-file, retry, batched DB writer |
| 1 | Codex | REQUEST_CHANGES | Over-scoped (4 concerns); DB writer ambiguity (batched coroutine vs run_in_executor); shutdown/resume underspecified; task fanout (100K tasks); resource limits incomplete |

### Changes in v2

1. **Scope narrowed**: Removed conditional fetching (Phase 6 → future spec 0022) and org prioritization (Phase 7 → future spec 0023). This spec covers only async pipeline + progress reporting.
2. **DNS pinning design added**: Explicit `AsyncHostPinCache` with `loop.run_in_executor` for `getaddrinfo` + custom `aiohttp.AbstractResolver`.
3. **DB writer ownership clarified**: Single `DBWriterActor` coroutine owns a bounded `asyncio.Queue`; download workers enqueue write requests; the actor batches and flushes via `run_in_executor`.
4. **Bounded org producer**: Replaced 100K-task fanout with an async iterator feeding a bounded worker pool.
5. **Graceful shutdown semantics defined**: Partial orgs are idempotent on resume; queued downloads are drained or abandoned (durability boundary = `crawled_orgs` upsert).
6. **AsyncHTTPClient lifecycle specified**: Async context manager; session created on `__aenter__`, closed on `__aexit__`.
7. **Retry semantics, halt-file polling, resource limits** all added as explicit ACs.
8. **AC11 pinned to stub-fetcher fixture; AC25 reframed as benchmark target.**

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
4. **Operational safety**: Same flock, resume, encryption-at-rest, halt-file, and retry behaviors as today
5. **Observability**: Real-time progress reporting (orgs/min, active connections, queue depths)

## Non-Goals

- Distributed crawling across multiple machines (future spec)
- Changing the discovery algorithm (subpage walking, candidate filtering)
- Modifying the classifier pipeline
- Changing the database schema (no new columns)
- Horizontal auto-scaling
- **Conditional fetching / ETag / If-Modified-Since** (future spec 0022)
- **Org prioritization / crawl scheduling** (future spec 0023)

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
  ├── Org Producer (async iterator, feeds bounded worker pool)
  │
  ├── Org Workers (pool of N coroutines, one per active org)
  │     └── Each: robots → homepage → subpages → candidate list
  │         └── Per-host semaphore: max 1 concurrent request per host, 3s gap
  │         └── Produces: Candidate objects → Download Queue
  │
  ├── Download Workers (pool of M coroutines pulling from queue)
  │     └── HEAD probe → filename score check → GET → validate → archive
  │         └── Per-host semaphore: shared with discovery
  │         └── Produces: write requests → DB Writer Queue
  │
  ├── DB Writer Actor (single coroutine, bounded input queue)
  │     └── Collects write requests, flushes via run_in_executor
  │         when batch reaches 50 rows or 5 seconds elapse
  │
  └── Halt-File Sentinel (background task, checks every 30s)
        └── If halt file appears, initiates graceful shutdown
```

### Key Design Decisions

**D1: aiohttp, not httpx.** `aiohttp` is battle-tested for high-concurrency crawlers (100K+ connection reuse), has native streaming support, and allows `auto_decompress=False` for manual decompressed-size enforcement. `httpx` wraps `anyio` which adds overhead we don't need.

**D2: Per-host async semaphore with reservation semantics.** Each unique host gets an `asyncio.Semaphore(1)` plus a timestamp tracking the last request. Before each request: acquire semaphore → compute delay since last request → `await asyncio.sleep(delay)` → make request → release semaphore. The semaphore serializes callers so the reservation pattern from the synchronous `HostThrottle` is preserved by construction — the semaphore holder is the only caller computing a delay. `asyncio.sleep()` yields to the event loop during the gap.

**D3: Producer-consumer with bounded queue.** Discovery and downloading are decoupled via an `asyncio.Queue(maxsize=1000)`. Discovery coroutines produce candidates; download coroutines consume them. The bounded queue provides natural backpressure — if downloads fall behind, discovery pauses. The queue holds `Candidate` objects (metadata only, ~200 bytes each), never PDF bodies.

**D4: Bounded org producer, not task-per-seed.** Instead of creating 100K `asyncio.Task` objects upfront (wasteful memory, slow cancellation), an async iterator yields `(ein, website)` tuples. A fixed pool of N org-worker coroutines pulls from the iterator via an `asyncio.Queue(maxsize=N)`. This bounds active tasks to N + M (download workers).

**D5: DB Writer Actor pattern.** A single `DBWriterActor` coroutine owns a bounded `asyncio.Queue(maxsize=200)`. Download workers (and discovery for `record_fetch`) enqueue `WriteRequest` dataclass instances. The actor drains the queue, batches rows, and flushes via `loop.run_in_executor(thread_pool, flush_batch, ...)` where `flush_batch` opens one `engine.begin()` transaction for the batch. The 4-thread executor is only used by this one actor — no unbounded work piling up.

**D6: Subprocess PDF validation via ProcessPoolExecutor.** The existing `_validate_pdf_structure` spawns a subprocess per PDF. In the async pipeline, download workers call `await loop.run_in_executor(process_pool, _validate_pdf_structure_inner, body)`. The `ProcessPoolExecutor(max_workers=4)` bounds concurrent validations. We use `_validate_pdf_structure_inner` directly (not the subprocess-spawning wrapper) since `ProcessPoolExecutor` already isolates in separate processes.

**D7: Custom aiohttp resolver for DNS pinning.** `AsyncHostPinCache` implements `aiohttp.abc.AbstractResolver`. On first resolution, it calls `socket.getaddrinfo` via `loop.run_in_executor` (non-blocking), checks `is_address_allowed`, and caches the result. Subsequent lookups return the pinned IP. The resolver is injected into `aiohttp.TCPConnector(resolver=async_pin_cache)`. This preserves the SSRF defense from `HostPinCache` (spec 0004 AC12.1) in the async context.

## Technical Implementation

### Phase 1: Async HTTP Client (`async_http_client.py`)

New module wrapping `aiohttp.ClientSession` with the same controls as `ReportsHTTPClient`:

```python
class AsyncHTTPClient:
    """Async context manager. Session created on enter, closed on exit."""

    async def __aenter__(self) -> AsyncHTTPClient: ...
    async def __aexit__(self, *exc) -> None: ...

    async def get(self, url: str, *, kind: str, seed_etld1: str | None) -> FetchResult:
        """Same return type as ReportsHTTPClient.get()."""

    async def head(self, url: str, *, kind: str) -> FetchResult:
        """Same return type as ReportsHTTPClient.head()."""
```

**Session configuration:**
- `aiohttp.ClientSession(connector=connector, headers=default_headers)`
- `connector = aiohttp.TCPConnector(limit=0, limit_per_host=0, use_dns_cache=False, resolver=async_pin_cache)`
  - `limit=0`, `limit_per_host=0`: no connector-level caps; our semaphore + org cap are the correctness contract
  - `use_dns_cache=False`: we do our own caching via `AsyncHostPinCache`
  - `resolver=async_pin_cache`: SSRF-safe DNS pinning
- `auto_decompress=False` on the session: we decompress manually to enforce the byte cap

**Decompressed-size cap:** Same streaming logic as `ReportsHTTPClient._decompress_stream`, using `resp.content.read(8192)` chunks with manual `zlib.decompressobj` for gzip.

**Redirect handling:** `allow_redirects=False` on each request; manual redirect following with `check_redirect_chain` at every hop, same as the synchronous client.

**Retry semantics:** Same policy as today (`config.RETRY_STATUSES`, `config.RETRY_KINDS`, `config.RETRY_MAX_ATTEMPTS`, `config.RETRY_BACKOFF_SEC`). Retries use `await asyncio.sleep(backoff)` instead of `time.sleep`.

### Phase 2: Async Host Throttle (`async_host_throttle.py`)

```python
class AsyncHostThrottle:
    """Per-host rate limiter using asyncio primitives.

    Thread-safe: NO — must be called from a single event loop.
    """

    def __init__(self, min_interval_sec: float = 3.0, jitter_sec: float = 0.5): ...

    async def wait(self, host: str) -> None:
        """Acquire host slot, sleep for politeness gap, return.

        Caller must call `release(host)` when the request completes.
        The semaphore ensures only one request per host is in-flight.
        The timestamp ensures >= min_interval_sec gap between requests.
        """

    def release(self, host: str) -> None:
        """Release the host slot after request completion."""
```

Lazy semaphore creation: semaphores are created on first access for a host (inside an `asyncio.Lock` to prevent races). This avoids pre-allocating semaphores for 100K hosts.

### Phase 3: Async DNS Pin Cache (`async_host_pin_cache.py`)

```python
class AsyncHostPinCache(aiohttp.abc.AbstractResolver):
    """DNS pin cache implementing aiohttp's resolver interface.

    Resolves hostname → IP via getaddrinfo in an executor (non-blocking),
    checks is_address_allowed, caches for the session lifetime.
    """

    async def resolve(self, host: str, port: int, family: int) -> list[dict]: ...
    async def close(self) -> None: ...
```

This is injected into `aiohttp.TCPConnector(resolver=...)`. The aiohttp connector calls `resolve` before each new TCP connection. Pinned IPs are returned for subsequent connections to the same host.

### Phase 4: Async Discovery (`async_discover.py`)

Adapts `per_org_candidates` for async I/O:

```python
async def discover_org(
    seed_url: str,
    seed_etld1: str,
    client: AsyncHTTPClient,
    robots_text: str,
    ein: str = "",
    db_writer_queue: asyncio.Queue | None = None,
) -> list[Candidate]:
```

The fetcher callback passed to `per_org_candidates` is currently synchronous. Two options:
- **(A) Rewrite `per_org_candidates` as async** — requires `async for` / `await` throughout. More invasive but cleaner.
- **(B) Write a new `async_per_org_candidates`** that reimplements the same logic with `await client.get()` calls.

**Decision: Option B.** The existing `per_org_candidates` stays untouched (used by the synchronous crawler). The new async version calls the same pure functions (`extract_candidates`, `classify_sitemap_url`, `_anchor_matches`, `_path_matches`) but awaits I/O. This doubles the discovery orchestration code (~150 lines) but avoids touching a well-tested module.

### Phase 5: Async Download (`async_fetch_pdf.py`)

```python
async def download(
    url: str,
    client: AsyncHTTPClient,
    *,
    seed_etld1: str | None = None,
    validate_structure: bool = True,
    process_pool: ProcessPoolExecutor | None = None,
) -> DownloadOutcome:
```

Same logic as `fetch_pdf.download`, but:
- `client.head()` / `client.get()` are awaited
- PDF structure validation: `await loop.run_in_executor(process_pool, _validate_pdf_structure_inner, body)`
- Returns the same `DownloadOutcome` dataclass

### Phase 6: DB Writer Actor (`async_db_writer.py`)

```python
@dataclass
class WriteRequest:
    """Union type for all DB write operations."""
    op: str  # "record_fetch" | "upsert_report" | "upsert_crawled_org"
    kwargs: dict

class DBWriterActor:
    """Single coroutine that owns all DB writes."""

    def __init__(self, engine: Engine, *, max_queue: int = 200, batch_size: int = 50,
                 flush_interval_sec: float = 5.0, executor_threads: int = 4): ...

    async def enqueue(self, request: WriteRequest) -> None:
        """Enqueue a write request. Blocks if queue is full (backpressure)."""

    async def run(self) -> None:
        """Main loop: drain queue, batch, flush via executor."""

    async def flush_and_stop(self) -> None:
        """Drain remaining items and shut down. Called during graceful shutdown."""
```

**Flush logic:** The actor collects `WriteRequest` objects. When the batch reaches `batch_size` or `flush_interval_sec` elapses (whichever comes first), it groups requests by operation type and calls the corresponding `db_writer.*` function in `run_in_executor`. Each flush opens one `engine.begin()` transaction per operation type.

### Phase 7: Async Crawler Orchestrator (`async_crawler.py`)

```python
async def run_async(
    engine: Engine,
    archive: Archive,
    seeds: list[tuple[str, str]],
    *,
    max_concurrent_orgs: int = 200,
    max_download_workers: int = 50,
    run_id: str = "",
    halt_dir: Path | None = None,
) -> CrawlStats:
```

**Bounded org processing:**
```python
async def org_producer(seeds, org_queue):
    """Feed seeds into a bounded queue. Backpressure when pool is full."""
    for ein, website in seeds:
        if should_skip_ein(engine, ein=ein, refresh=refresh):
            continue
        await org_queue.put((ein, website))
    # Signal completion
    for _ in range(max_concurrent_orgs):
        await org_queue.put(None)

async def org_worker(org_queue, download_queue, client, db_actor):
    """Pull orgs from queue, discover candidates, feed download queue."""
    while True:
        item = await org_queue.get()
        if item is None:
            break
        ein, website = item
        try:
            candidates = await discover_org(...)
            for cand in candidates:
                await download_queue.put((ein, cand, ...))
        except Exception:
            logger.exception(...)
        finally:
            await db_actor.enqueue(WriteRequest("upsert_crawled_org", {...}))
            org_queue.task_done()
```

**Halt-file sentinel:**
```python
async def halt_sentinel(halt_dir, shutdown_event):
    """Check for halt files every 30 seconds."""
    while not shutdown_event.is_set():
        if any(halt_dir.glob("HALT-*.md")):
            shutdown_event.set()
            return
        await asyncio.sleep(30)
```

**SIGINT/SIGTERM handling:**
```python
loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
```

When `shutdown_event` is set:
1. Stop feeding new orgs (producer exits)
2. Let in-flight org workers finish their current org (bounded by org completion time)
3. Drain the download queue (or discard if `--fast-shutdown`)
4. Call `db_actor.flush_and_stop()` — all queued writes flush
5. Close `AsyncHTTPClient` (closes `aiohttp.ClientSession`)
6. Exit cleanly

**Durability boundary:** An org is considered "complete" only after its `upsert_crawled_org` write is flushed. On resume, any org without a `crawled_orgs` row will be re-processed from scratch. This is idempotent because `upsert_report` uses `ON CONFLICT (content_sha256)`.

### Phase 8: Progress Reporting

A background coroutine logs stats every 60 seconds:

```
[12:34:56] orgs: 1,234/100,000 (1.2%) | active: 187 | download_q: 423 | PDFs: 892 | rate: 512 orgs/hr | ETA: 8d 3h
```

Stats are tracked via an `asyncio`-safe `CrawlStats` dataclass with atomic counters. No Unix socket (removed per Codex security concern) — log-only for now; future dashboard spec can add socket reporting.

### CLI Integration

The existing `crawler.py:run()` function gains an `--async` flag:

```bash
# Synchronous (existing, for debugging / comparison)
python -m lavandula.reports.crawler --max-workers 8 ...

# Async (new, for production)
python -m lavandula.reports.crawler --async --max-concurrent-orgs 200 ...
```

Both modes share the same flock, so they're mutually exclusive. The `--async` flag is incompatible with `--max-workers`.

Once validated, async becomes the default and the flag is inverted to `--sync`.

## Acceptance Criteria

### Core Async Infrastructure
- **AC1**: `AsyncHTTPClient` is an async context manager. Entering creates an `aiohttp.ClientSession`; exiting closes it. Using `get()`/`head()` outside the context raises `RuntimeError`.
- **AC2**: `AsyncHTTPClient.get()` returns `FetchResult` with identical fields to `ReportsHTTPClient.get()` for the same URL, verified by a parity test against a deterministic stub server.
- **AC3**: `AsyncHTTPClient` enforces the decompressed-byte cap using `auto_decompress=False` and manual `zlib` decompression (same streaming logic as spec 0004 AC8).
- **AC4**: `AsyncHTTPClient` applies every-hop redirect gating via `check_redirect_chain`, verified by a test with a multi-hop redirect fixture.
- **AC5**: `AsyncHTTPClient` strips Referer, sets User-Agent and Accept-Encoding per config, normalizes protocol-relative URLs (`//` → `https://`).
- **AC6**: Per-host async throttle enforces >= 3s gap between requests to the same host, verified by unit test with mock event loop clock (`loop.time()`).
- **AC7**: Retry semantics match the synchronous client: same `RETRY_STATUSES`, `RETRY_KINDS`, `RETRY_MAX_ATTEMPTS`, `RETRY_BACKOFF_SEC`. Retries use `asyncio.sleep`.

### DNS Pinning (SSRF Defense)
- **AC8**: `AsyncHostPinCache` implements `aiohttp.abc.AbstractResolver`. First resolution calls `socket.getaddrinfo` via `run_in_executor` (non-blocking). Result is cached for the session lifetime.
- **AC9**: `AsyncHostPinCache.resolve()` rejects hosts that resolve to disallowed addresses (private, loopback, cloud-metadata) via `is_address_allowed`, raising `aiohttp.ClientConnectorError`.
- **AC10**: `AsyncHTTPClient` uses `AsyncHostPinCache` as the connector's resolver. A test verifies that a hostname resolving to `127.0.0.1` is rejected.

### Pipeline Architecture
- **AC11**: Discovery and download run as separate coroutine pools connected by a bounded `asyncio.Queue(maxsize=1000)`.
- **AC12**: Active orgs are bounded by `--max-concurrent-orgs` (default 200). Implemented via a bounded producer queue, not task-per-seed.
- **AC13**: Download workers (default 50) pull from the shared queue. Worker count configurable via `--max-download-workers`.
- **AC14**: `DBWriterActor` is a single coroutine owning a bounded queue (maxsize=200). Download workers and discovery coroutines enqueue `WriteRequest` objects. The actor batches (up to 50 rows) and flushes via `run_in_executor` on a 4-thread pool.
- **AC15**: PDF structure validation runs via `run_in_executor` on a `ProcessPoolExecutor(max_workers=4)`.

### Correctness
- **AC16**: For a deterministic stub-fetcher fixture returning canned HTML/robots responses, the async crawler produces the same set of candidate URLs and the same set of archived PDFs (by SHA256) as the synchronous crawler.
- **AC17**: All existing unit tests for `candidate_filter`, `discover`, `fetch_pdf`, `redirect_policy`, and `db_writer` continue to pass without modification.
- **AC18**: The async crawler respects robots.txt identically to the synchronous crawler.
- **AC19**: The async crawler applies the same filename scoring, taxonomy-driven filtering, and TICK-001 relaxation as the synchronous crawler.

### Operational Safety
- **AC20**: Flock (spec 0004 AC19) prevents concurrent async and synchronous crawler instances. The same lock file is used.
- **AC21**: Resume semantics work identically — already-crawled EINs (those with a `crawled_orgs` row) are skipped unless `--refresh`.
- **AC22**: Encryption-at-rest check and TLS self-test run before the event loop starts.
- **AC23**: A halt-file sentinel checks `config.HALT` every 30 seconds. If a `HALT-*.md` file appears, `shutdown_event` is set and graceful shutdown begins.
- **AC24**: Graceful shutdown on SIGINT/SIGTERM/halt-file: (a) stop accepting new orgs, (b) let in-flight org workers finish their current org, (c) drain the download queue, (d) flush all pending DB writes via `DBWriterActor.flush_and_stop()`, (e) close the HTTP client, (f) exit 0.
- **AC25**: Partial orgs (interrupted between discovery and `upsert_crawled_org`) are re-processed on resume. `upsert_report` is idempotent on `content_sha256`, so duplicate downloads produce no duplicate rows.

### Observability
- **AC26**: Progress stats are logged every 60 seconds: orgs completed/total, active count, download queue depth, PDFs found, rate (orgs/hr), estimated time remaining.
- **AC27**: A final summary log line reports total orgs, total PDFs, total bytes, wall-clock time, and effective orgs/hr rate.

### Resource Limits
- **AC28**: Total active `asyncio.Task` objects at any time is bounded by `max_concurrent_orgs + max_download_workers + 4` (producer, DB actor, halt sentinel, progress reporter). No unbounded task creation.
- **AC29**: Download queue maxsize = 1000. DB writer queue maxsize = 200. Both provide backpressure (producers block when full).
- **AC30**: Request timeout per fetch is `config.REQUEST_TIMEOUT_SEC` (30s), enforced via `aiohttp.ClientTimeout(total=30)`.

### Performance (Benchmark, not gate)
- **AC31**: On the same 100-org test set used in the 2026-04-25 synchronous run, the async crawler completes in < 30 minutes wall-clock. This is a benchmark target, not a hard pass/fail gate — network conditions vary.
- **AC32**: Peak RSS stays under 2 GB during a 1000-org crawl, measured by `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` logged at shutdown.

## Traps to Avoid

1. **Don't bypass per-host throttling for speed.** We go faster by multiplexing across hosts, not by hammering any single host. The 3s gap is a policy commitment.

2. **Don't use `asyncpg` or async SQLAlchemy.** The DB writes are lightweight and infrequent compared to HTTP I/O. `run_in_executor` with a 4-thread pool is sufficient and avoids rewriting the entire DB layer.

3. **Don't load all PDF bodies into memory.** Download workers archive each PDF before pulling the next candidate from the queue. The queue holds `Candidate` objects (metadata only, ~200 bytes each), not PDF bodies.

4. **Don't forget `aiohttp` session cleanup.** `AsyncHTTPClient` is an async context manager. Forgetting `async with` leaks TCP connections. AC1 enforces this.

5. **Don't assume DNS is fast.** `AsyncHostPinCache` runs `getaddrinfo` via `run_in_executor` so it doesn't block the event loop. `aiodns` is not required — the executor approach is simpler and `getaddrinfo` is cached by the OS.

6. **Don't create tasks eagerly.** Use the bounded producer pattern (D4), not `asyncio.create_task` per seed. 100K tasks × ~1 KB each = 100 MB of task overhead alone.

7. **Don't forget `asyncio.CancelledError` handling.** On shutdown, pending coroutines may be cancelled. Each coroutine must handle cancellation gracefully: close HTTP responses, don't leave partial DB writes. The `DBWriterActor.flush_and_stop()` ensures queued writes are committed.

8. **`aiohttp` auto-decompresses by default.** Set `auto_decompress=False` on the session to enforce the decompressed-byte cap manually. Forgetting this bypasses the gzip-bomb defense.

9. **`asyncio.Lock()` vs `threading.Lock()`.** The async throttle and pin cache use `asyncio.Lock` (not `threading.Lock`). They are single-event-loop only. The DB writer's executor uses threading internally, but the actor pattern serializes access.

## Security Considerations

- **SSRF protections preserved.** `AsyncHostPinCache` (D7) provides the same DNS-pinning defense as `HostPinCache`. `check_redirect_chain` and `is_address_allowed` are called identically.
- **No new network exposure.** The async crawler is a client-only change; no new listening sockets or inbound connections. Unix socket for dashboard reporting is out of scope (removed per Codex review).
- **Connection limits prevent resource exhaustion.** Active tasks bounded by AC28. Queue sizes bounded by AC29. Connector-level limits are set to 0 (uncapped) because our application-level controls are the correctness contract; connector limits would be redundant and harder to reason about.
- **Graceful shutdown prevents data corruption.** DB writer flushes before exit (AC24). Flock prevents concurrent instances (AC20). Resume is idempotent (AC25).

## Testing Strategy

1. **Unit tests**: Async HTTP client with mocked `aiohttp` responses (using `aioresponses` or manual `AsyncMock`). Async throttle with mock `loop.time()`. `AsyncHostPinCache` with mock `getaddrinfo`. Queue backpressure tests.
2. **Integration tests**: Full async pipeline with deterministic stub fetcher. Verify candidate + PDF SHA256 parity with synchronous crawler output (AC16).
3. **Backpressure test**: Slow stub DB writer, verify download queue fills to maxsize and producers block without OOM.
4. **Cancellation tests**: SIGINT mid-crawl → verify DB writes flushed, no partial state. Halt-file appearance → same. `--limit` reached → same.
5. **Performance benchmark**: 100-org set with real network. Capture wall-clock time. Compare against synchronous baseline.
6. **Memory benchmark**: 1000-org crawl, log peak RSS at shutdown (AC32).

## Migration Strategy

The async crawler is a **new set of modules** alongside the existing synchronous ones:
- `async_http_client.py` (new, alongside `http_client.py`)
- `async_host_throttle.py` (new, alongside `host_throttle.py`)
- `async_host_pin_cache.py` (new, extends `url_guard.py` concepts)
- `async_discover.py` (new, wraps same logic as `discover.py`)
- `async_fetch_pdf.py` (new, wraps same logic as `fetch_pdf.py`)
- `async_db_writer.py` (new, actor pattern over `db_writer.py`)
- `async_crawler.py` (new, alongside `crawler.py`)

Shared (unchanged):
- `candidate_filter.py`, `config.py`, `taxonomy.py`, `filename_grader.py`
- `redirect_policy.py`, `url_guard.py`, `url_redact.py`
- `db_writer.py`, `robots.py`, `sitemap.py`
- `pdf_extract.py`, `logging_utils.py`

**Rollback:** Remove `--async` flag. The synchronous `crawler.py` is always available.

## Estimated Effort

- Phase 1 (async HTTP client): 250-350 lines
- Phase 2 (async throttle): 50-80 lines
- Phase 3 (async DNS pin cache): 60-100 lines
- Phase 4 (async discovery): 150-200 lines
- Phase 5 (async download): 80-120 lines
- Phase 6 (DB writer actor): 100-150 lines
- Phase 7 (orchestrator): 250-350 lines
- Phase 8 (progress reporting): 50-80 lines
- Tests: 400-600 lines
- **Total**: ~1400-2000 lines of new code, ~20 lines modified in `crawler.py` (CLI flag)

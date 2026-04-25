# Spec 0021 — Async I/O Crawler Pipeline

**Status**: Approved
**Author**: Architect
**Created**: 2026-04-25
**Dependencies**: 0004 (site-crawl catalogue), 0020 (data-driven taxonomy)

---

## Consultation Log

| Round | Model | Type | Verdict | Key Issues |
|-------|-------|------|---------|------------|
| 1 | Claude | spec-review | REQUEST_CHANGES | DNS pinning under aiohttp; scope bundles 3 features; missing ACs for halt-file, retry, DB writer |
| 1 | Codex | spec-review | REQUEST_CHANGES | Over-scoped; DB writer ambiguity; shutdown/resume underspecified; task fanout |
| 2 | Codex | red-team | REQUEST_CHANGES | Org-completion durability; shutdown drain/abandon inconsistency; DNS multi-address underspecified |
| 2 | Claude | red-team | REQUEST_CHANGES | Non-gzip Content-Encoding bypass; TLS hostname verification; ProcessPool worker tainting; denial-of-crawl via transient errors |

### Changes in v2 (post spec-review)

1. **Scope narrowed**: Removed conditional fetching (→ spec 0022) and org prioritization (→ spec 0023).
2. **DNS pinning design added**: `AsyncHostPinCache` as `aiohttp.AbstractResolver`.
3. **DB writer ownership clarified**: Single `DBWriterActor` coroutine.
4. **Bounded org producer**: Replaced 100K-task fanout.
5. **Graceful shutdown semantics defined**.
6. **AsyncHTTPClient lifecycle specified**: Async context manager.
7. **Retry semantics, halt-file polling, resource limits** added as ACs.

### Changes in v3 (post red-team)

1. **Content-Encoding defense**: Explicit AC rejecting any encoding other than `gzip` or `identity`/absent. `auto_decompress=False` + manual zlib. Matches sync client behavior (spec 0004 AC8).
2. **TLS hostname verification AC**: Integration test verifying cert-for-hostname, not cert-for-IP, when using custom resolver.
3. **ProcessPool worker recycling**: `max_tasks_per_child=1` preserves subprocess-per-PDF isolation from sync client.
4. **PDF validation timeout**: 30s per-task via `Future.result(timeout=30)` + process termination on timeout.
5. **Org completion semantics**: Per-org `asyncio.Event` fires only after all downloads for that org are confirmed archived + DB-flushed. `upsert_crawled_org` enqueued only after the barrier.
6. **Transient vs permanent failure**: Orgs with transient failures (network, timeout, TLS) are NOT marked complete — resume retries them. Only permanent failures (robots disallow, SSRF rejection) and successful crawls are marked.
7. **DNS pinning tightened**: Single IP pinned (first IPv4 result preferred). Negative results cached. Multi-address hosts use only the pinned IP. Each new host in redirect chain is independently resolved.
8. **Shutdown semantics unified**: Single mode — drain in-flight, flush DB, exit. No `--fast-shutdown`. Double-SIGINT within 5s forces immediate exit.
9. **Connector defense-in-depth**: `limit=500, limit_per_host=2` as safety net behind application-level controls.
10. **Memory accounting**: Max concurrent PDF bodies = `max_download_workers` x `MAX_PDF_BYTES` = 50 x 50MB = 2.5GB. Reduced default `max_download_workers` to 20 for 1GB peak PDF body budget.
11. **DB executor**: Single-thread executor for DBWriterActor (Postgres handles concurrency, but single-thread simplifies ordering).
12. **Keepalive timeout**: `keepalive_timeout=60` on connector; connections recycled periodically.
13. **Granular request timeouts**: `ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=15)`.

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

**D4: Bounded org producer, not task-per-seed.** Instead of creating 100K `asyncio.Task` objects upfront (wasteful memory, slow cancellation), an async iterator yields `(ein, website)` tuples. A fixed pool of N org-worker coroutines pulls from the iterator via an `asyncio.Queue(maxsize=N)`. This bounds application-created tasks to N + M + 4 (org workers + download workers + producer/actor/sentinel/reporter).

**D5: DB Writer Actor pattern.** A single `DBWriterActor` coroutine owns a bounded `asyncio.Queue(maxsize=200)`. All DB writes flow through it — download workers, discovery coroutines, and org-completion all enqueue typed `WriteRequest` dataclass instances (discriminated union: `RecordFetchRequest`, `UpsertReportRequest`, `UpsertCrawledOrgRequest`). The actor drains the queue, batches rows by operation type, and flushes via `loop.run_in_executor(single_thread_executor, flush_batch, ...)`. A single-thread executor simplifies ordering and avoids contention. Flush failures are logged and retried once; persistent failures are logged with the full request for manual recovery.

**D6: ProcessPoolExecutor with per-task recycling.** Download workers call `await loop.run_in_executor(process_pool, _validate_pdf_structure_inner, body)`. The pool is configured as `ProcessPoolExecutor(max_workers=4, max_tasks_per_child=1)` — each worker process handles exactly one PDF then exits, preserving the subprocess-per-PDF isolation of the synchronous client. This prevents a malicious PDF that corrupts parser state from tainting subsequent validations. Cost: ~10ms fork overhead per validation, negligible vs network I/O. Additionally, `Future.result(timeout=30)` ensures hostile PDFs cannot hang the validation pool; on timeout the worker is terminated.

**D7: Custom aiohttp resolver for DNS pinning.** `AsyncHostPinCache` implements `aiohttp.abc.AbstractResolver`. On first resolution, it calls `socket.getaddrinfo` via `loop.run_in_executor` (non-blocking), selects the first IPv4 result (AF_INET preferred for consistency; falls back to IPv6 if no IPv4), checks `is_address_allowed`, and caches the single pinned IP. Negative results (disallowed IPs) are also cached for the session lifetime to prevent CPU-amplification via repeated resolution of private-IP hosts. Each new hostname encountered in a redirect chain is independently resolved and validated. The resolver preserves the original hostname in the result dict so aiohttp uses it for SNI and TLS certificate validation (not the IP). The resolver is injected into `aiohttp.TCPConnector(resolver=async_pin_cache)`.

**D8: Transient vs permanent org failures.** An org's crawl outcome is categorized:
- **success**: Discovery + all downloads completed and archived. `upsert_crawled_org` records it; resume skips.
- **permanent_skip**: Robots disallowed, SSRF rejection, invalid seed URL. Recorded with status; resume skips.
- **transient_failure**: Network timeout, TLS error, DNS failure, server error. NOT recorded in `crawled_orgs`; resume retries.
This prevents an adversary from using reproducible errors to permanently opt out of crawling (denial-of-crawl defense).

**D9: Org completion barrier.** Each org tracks its outstanding download count via an `asyncio.Event` + atomic counter. The org worker:
1. Discovers candidates and enqueues them to the download queue (incrementing the counter per enqueue)
2. Waits on the barrier event (set when counter reaches 0)
3. Only then enqueues `UpsertCrawledOrgRequest` to the DB writer
4. The download worker decrements the counter after archiving + DB write confirmation

This ensures `upsert_crawled_org` is never written until all downloads for that org are durably persisted.

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
- `connector = aiohttp.TCPConnector(limit=500, limit_per_host=2, use_dns_cache=False, resolver=async_pin_cache, keepalive_timeout=60)`
  - `limit=500`: defense-in-depth cap behind application-level org/semaphore controls
  - `limit_per_host=2`: safety net behind the per-host semaphore (1 active + 1 queued)
  - `use_dns_cache=False`: we do our own caching via `AsyncHostPinCache`
  - `resolver=async_pin_cache`: SSRF-safe DNS pinning (D7)
  - `keepalive_timeout=60`: connections recycled periodically; prevents stale pinned connections
- `auto_decompress=False` on the session: we decompress manually to enforce the byte cap
- `timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=15)`: granular timeouts catch slow-loris-style trickle reads

**Content-Encoding policy (security-critical):** Only `gzip` and `identity`/absent are accepted. If the response has `Content-Encoding` set to anything else (`br`, `deflate`, `zstd`, etc.), the response is rejected as `blocked_content_type`. This matches the synchronous client (spec 0004 AC8) and the outbound `Accept-Encoding: gzip, identity` header — any other encoding is a protocol violation. The decompressed-byte cap is enforced via manual `zlib.decompressobj(zlib.MAX_WBITS | 16)` for gzip, using `resp.content.read(8192)` chunks. SHA256 is always computed over decompressed bytes.

**Redirect handling:** `allow_redirects=False` on each request; manual redirect following with `check_redirect_chain` at every hop, same as the synchronous client. Cookie and Authorization headers are not forwarded on cross-origin redirects (inherited from `check_redirect_chain` which rejects cross-eTLD+1 hops for non-platform domains).

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
    Negative results (disallowed IPs) are also cached.
    """

    async def resolve(self, host: str, port: int, family: int) -> list[dict]:
        """Return pinned IP for host. Raises on disallowed/unresolvable.

        - Prefers IPv4 (AF_INET) results; falls back to IPv6 if no IPv4.
        - Pins exactly ONE IP per hostname for the session.
        - Negative results cached — repeated attempts to resolve a
          private-IP host do not trigger new getaddrinfo calls.
        - Preserves original hostname in result dict for SNI/cert validation.
        """

    async def close(self) -> None: ...
```

**Contract:**
- `getaddrinfo` runs via `loop.run_in_executor` (non-blocking).
- First IPv4 result is pinned. If no IPv4 results, first IPv6 result is pinned.
- `is_address_allowed(pinned_ip)` must return True; otherwise raises `aiohttp.ClientConnectorError` and the rejection is cached.
- The resolver result dict includes `hostname=original_host` so aiohttp uses it for TLS SNI and certificate verification, NOT the IP address.
- Each hostname in a redirect chain is independently resolved and validated. Redirect to a new host triggers a fresh `resolve()` call.
- Pooled connections use `keepalive_timeout=60` so stale connections don't persist indefinitely.

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
    max_download_workers: int = 20,
    run_id: str = "",
    halt_dir: Path | None = None,
) -> CrawlStats:
```

**Bounded org processing:**
```python
async def org_producer(seeds, org_queue, shutdown_event):
    """Feed seeds into a bounded queue. Backpressure when pool is full."""
    for ein, website in seeds:
        if shutdown_event.is_set():
            break
        if should_skip_ein(engine, ein=ein, refresh=refresh):
            continue
        await org_queue.put((ein, website))
    # Signal completion
    for _ in range(max_concurrent_orgs):
        await org_queue.put(None)

async def org_worker(org_queue, download_queue, client, db_actor):
    """Pull orgs from queue, discover candidates, wait for downloads, mark complete."""
    while True:
        item = await org_queue.get()
        if item is None:
            break
        ein, website = item
        org_tracker = OrgDownloadTracker()
        try:
            candidates = await discover_org(...)
            for cand in candidates:
                org_tracker.increment()
                await download_queue.put((ein, cand, org_tracker))
            # Wait for ALL downloads for this org to complete (D9 barrier)
            await org_tracker.wait_all_done()
            # Only now mark the org as complete
            await db_actor.enqueue(UpsertCrawledOrgRequest(ein=ein, ...))
        except (asyncio.CancelledError, Exception) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise  # propagate cancellation
            # Classify: transient or permanent?
            if _is_transient(exc):
                logger.warning("transient failure ein=%s: %s", ein, exc)
                # Do NOT enqueue upsert_crawled_org — resume will retry
            else:
                logger.exception("permanent failure ein=%s", ein, exc)
                await db_actor.enqueue(UpsertCrawledOrgRequest(
                    ein=ein, status="permanent_skip", ...))
        finally:
            org_queue.task_done()
```

**OrgDownloadTracker (D9 barrier):**
```python
class OrgDownloadTracker:
    """Tracks outstanding downloads for one org."""
    def __init__(self):
        self._pending = 0
        self._done = asyncio.Event()
        self._done.set()  # initially done (0 pending)
    def increment(self): ...
    def decrement(self): ...  # sets event when pending reaches 0
    async def wait_all_done(self): await self._done.wait()
```

Download workers call `org_tracker.decrement()` after archiving + confirming the DB write.

**Halt-file sentinel:**
```python
async def halt_sentinel(halt_dir, shutdown_event):
    """Check for halt files every 30 seconds.
    halt_dir permissions validated at startup (must not be world-writable).
    """
    while not shutdown_event.is_set():
        if any(halt_dir.glob("HALT-*.md")):
            shutdown_event.set()
            return
        await asyncio.sleep(30)
```

**Signal handling:**
```python
_sigint_count = 0
def _on_sigint():
    nonlocal _sigint_count
    _sigint_count += 1
    if _sigint_count >= 2:
        logger.warning("double SIGINT — force exit")
        os._exit(1)
    shutdown_event.set()

loop.add_signal_handler(signal.SIGINT, _on_sigint)
loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
```

**Graceful shutdown sequence** (single mode, no `--fast-shutdown`):
1. Stop feeding new orgs (producer checks `shutdown_event`)
2. Let in-flight org workers finish their current org. Each org's barrier ensures its downloads complete.
3. Drain remaining items from the download queue (in-flight downloads complete, queued ones are processed)
4. Call `db_actor.flush_and_stop()` — `flush_and_stop` is shielded from cancellation (`asyncio.shield`)
5. Close `AsyncHTTPClient` (closes `aiohttp.ClientSession`)
6. Exit 0

Double-SIGINT within 5 seconds forces `os._exit(1)`, accepting that in-flight DB writes may be lost.

**Durability boundary:** An org is considered "complete" only after:
- All its downloads are archived to S3
- All corresponding `upsert_report` writes are flushed by the DB actor
- The `upsert_crawled_org` write is flushed

On resume, any org without a `crawled_orgs` row is re-processed from scratch. `upsert_report` uses `ON CONFLICT (content_sha256)` so duplicate downloads produce no duplicate rows. Orgs with transient failures have no `crawled_orgs` row and will be retried.

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
- **AC3**: `AsyncHTTPClient` sets `auto_decompress=False` and manually decompresses gzip via `zlib.decompressobj`. Decompressed-byte cap is enforced identically to spec 0004 AC8.
- **AC4**: If `Content-Encoding` is anything other than `gzip` or `identity`/absent, the response is rejected as `blocked_content_type`. Brotli, deflate, and zstd are explicitly NOT supported. This matches the outbound `Accept-Encoding: gzip, identity` header.
- **AC5**: `AsyncHTTPClient` applies every-hop redirect gating via `check_redirect_chain`, verified by a test with a multi-hop redirect fixture.
- **AC6**: `AsyncHTTPClient` strips Referer, sets User-Agent and Accept-Encoding per config, normalizes protocol-relative URLs (`//` → `https://`). Cookie/Authorization headers are not forwarded on cross-origin redirects.
- **AC7**: Per-host async throttle enforces >= 3s gap between requests to the same host, verified by unit test with mock event loop clock (`loop.time()`).
- **AC8**: Retry semantics match the synchronous client: same `RETRY_STATUSES`, `RETRY_KINDS`, `RETRY_MAX_ATTEMPTS`, `RETRY_BACKOFF_SEC`. Retries use `asyncio.sleep`.
- **AC9**: Granular request timeouts: `total=30, connect=10, sock_connect=10, sock_read=15`. Verified by test with a slow-responding stub.

### DNS Pinning (SSRF Defense)
- **AC10**: `AsyncHostPinCache` implements `aiohttp.abc.AbstractResolver`. First resolution calls `socket.getaddrinfo` via `run_in_executor` (non-blocking). Prefers first IPv4 result; falls back to IPv6. Exactly one IP is pinned per hostname.
- **AC11**: `AsyncHostPinCache.resolve()` rejects hosts resolving to disallowed addresses via `is_address_allowed`, raising `aiohttp.ClientConnectorError`. Negative results (rejections) are cached for the session lifetime — no repeated `getaddrinfo` for known-bad hosts.
- **AC12**: The resolver result preserves the original hostname for TLS SNI and certificate validation. An integration test verifies: when `resolve()` returns IP X for hostname `goodhost.example`, and the TLS server at X presents a cert for `evilhost.example`, the connection FAILS with a hostname-verification error.
- **AC13**: `AsyncHTTPClient` uses `AsyncHostPinCache` as the connector's resolver. A test verifies that a hostname resolving to `127.0.0.1` is rejected.
- **AC14**: Each new hostname in a redirect chain triggers an independent `resolve()` call and `is_address_allowed` check.

### Pipeline Architecture
- **AC15**: Discovery and download run as separate coroutine pools connected by a bounded `asyncio.Queue(maxsize=1000)`.
- **AC16**: Active orgs are bounded by `--max-concurrent-orgs` (default 200). Implemented via a bounded producer queue, not task-per-seed.
- **AC17**: Download workers (default 20) pull from the shared queue. Worker count configurable via `--max-download-workers`.
- **AC18**: `DBWriterActor` is a single coroutine owning a bounded queue (maxsize=200). Write requests are typed dataclasses (`RecordFetchRequest`, `UpsertReportRequest`, `UpsertCrawledOrgRequest`). The actor batches (up to 50 rows) and flushes via `run_in_executor` on a single-thread executor.
- **AC19**: If a DB flush fails, the actor retries once. Persistent failures are logged with the full request payload for manual recovery. The actor does not crash on DB errors.
- **AC20**: PDF structure validation runs via `run_in_executor` on a `ProcessPoolExecutor(max_workers=4, max_tasks_per_child=1)`. Worker processes are recycled after each task to prevent cross-PDF state corruption.
- **AC21**: PDF validation has a 30-second timeout. On timeout, the worker is terminated and the candidate is recorded as a validation failure (`pdf_structure_timeout`).

### Org Completion (Durability)
- **AC22**: `upsert_crawled_org` is enqueued to the DB writer ONLY after all downloads for that org have been archived and their corresponding `upsert_report` writes have been enqueued. The per-org barrier (D9) enforces this.
- **AC23**: Orgs with transient failures (network timeout, TLS error, DNS failure, server error) are NOT marked in `crawled_orgs`. Resume retries them.
- **AC24**: Orgs with permanent failures (robots disallow, SSRF rejection, invalid seed) are marked with a `permanent_skip` status. Resume skips them.
- **AC25**: Successfully crawled orgs are marked in `crawled_orgs` with counts. Resume skips them.

### Correctness
- **AC26**: For a deterministic stub-fetcher fixture returning canned HTML/robots responses, the async crawler produces the same set of candidate URLs and the same set of archived PDFs (by SHA256) as the synchronous crawler.
- **AC27**: All existing unit tests for `candidate_filter`, `discover`, `fetch_pdf`, `redirect_policy`, and `db_writer` continue to pass without modification.
- **AC28**: The async crawler respects robots.txt identically to the synchronous crawler.
- **AC29**: The async crawler applies the same filename scoring, taxonomy-driven filtering, and TICK-001 relaxation as the synchronous crawler.

### Operational Safety
- **AC30**: Flock (spec 0004 AC19) prevents concurrent async and synchronous crawler instances. Same lock file.
- **AC31**: Resume semantics: already-crawled EINs (those with a `crawled_orgs` row with status != `permanent_skip` if desired) are skipped unless `--refresh`.
- **AC32**: Encryption-at-rest check and TLS self-test run before the event loop starts.
- **AC33**: Halt-file sentinel checks `config.HALT` every 30 seconds. Halt-dir permissions validated at startup (refuse to start if world-writable).
- **AC34**: Graceful shutdown on SIGINT/SIGTERM/halt-file: (a) stop accepting new orgs, (b) let in-flight org workers finish their current org (including download barrier), (c) drain remaining download queue items, (d) `DBWriterActor.flush_and_stop()` (shielded from cancellation), (e) close HTTP client, (f) exit 0.
- **AC35**: Double-SIGINT within 5 seconds forces `os._exit(1)`, accepting that in-flight DB writes may be lost.

### Observability
- **AC36**: Progress stats are logged every 60 seconds: orgs completed/total, active count, download queue depth, PDFs found, rate (orgs/hr), estimated time remaining.
- **AC37**: A final summary log line reports total orgs, total PDFs, total bytes, wall-clock time, effective orgs/hr, and peak RSS.

### Resource Limits
- **AC38**: Application-created `asyncio.Task` count is bounded by `max_concurrent_orgs + max_download_workers + 4` (producer, DB actor, halt sentinel, progress reporter). No task-per-seed fanout.
- **AC39**: Download queue maxsize = 1000. DB writer queue maxsize = 200. Both provide backpressure.
- **AC40**: Connector defense-in-depth: `limit=500, limit_per_host=2`. These are safety nets; the per-host semaphore and org cap are the primary controls.
- **AC41**: Max concurrent PDF bodies in memory = `max_download_workers` x `MAX_PDF_BYTES` = 20 x 50MB = 1 GB. This fits within the 2 GB RSS target.

### Performance (Benchmark, not gate)
- **AC42**: On the same 100-org test set used in the 2026-04-25 synchronous run, the async crawler completes in < 30 minutes wall-clock. Benchmark target, not hard gate.
- **AC43**: Peak RSS stays under 2 GB during a 1000-org crawl, measured by `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` logged at shutdown.

## Traps to Avoid

1. **Don't bypass per-host throttling for speed.** We go faster by multiplexing across hosts, not by hammering any single host. The 3s gap is a policy commitment.

2. **Don't use `asyncpg` or async SQLAlchemy.** The DB writes are lightweight and infrequent compared to HTTP I/O. `run_in_executor` with a single-thread pool is sufficient and avoids rewriting the entire DB layer.

3. **Don't load all PDF bodies into memory.** Download workers archive each PDF before pulling the next candidate from the queue. The queue holds `Candidate` objects (metadata only, ~200 bytes each), not PDF bodies. Max concurrent bodies = `max_download_workers` (20) x `MAX_PDF_BYTES` (50MB) = 1 GB.

4. **Don't forget `aiohttp` session cleanup.** `AsyncHTTPClient` is an async context manager. Forgetting `async with` leaks TCP connections. AC1 enforces this.

5. **Don't assume DNS is fast.** `AsyncHostPinCache` runs `getaddrinfo` via `run_in_executor` so it doesn't block the event loop. `aiodns` is NOT a dependency — the executor approach is simpler and `getaddrinfo` is cached by the OS.

6. **Don't create tasks eagerly.** Use the bounded producer pattern (D4), not `asyncio.create_task` per seed. 100K tasks x ~1 KB each = 100 MB of task overhead alone.

7. **Don't forget `asyncio.CancelledError` handling.** On shutdown, pending coroutines may be cancelled. Cancellation protocol: org workers finish cooperatively (not cancelled mid-org); download workers may be cancelled after their current item; `DBWriterActor.flush_and_stop()` is shielded from cancellation via `asyncio.shield`.

8. **`aiohttp` auto-decompresses by default.** Set `auto_decompress=False` on the session. Forgetting this bypasses the gzip-bomb defense AND allows non-gzip encodings (brotli, zstd) to slip through without manual rejection.

9. **`asyncio.Lock()` vs `threading.Lock()`.** The async throttle and pin cache use `asyncio.Lock` (not `threading.Lock`). They are single-event-loop only. Lazy-initialize locks inside coroutines if needed to avoid event-loop binding issues.

10. **Don't mark orgs complete on transient failures.** A bare `except Exception` that records the org as crawled enables denial-of-crawl. Classify failures (D8) and only record permanent outcomes.

11. **Don't skip `max_tasks_per_child=1` on the ProcessPool.** Without it, a malicious PDF can corrupt worker state and taint all subsequent validations. The ~10ms fork overhead is negligible vs network I/O.

## Security Considerations

- **SSRF protections preserved.** `AsyncHostPinCache` (D7) provides the same DNS-pinning defense as `HostPinCache`, with explicit IPv4 preference, negative caching, and hostname-based TLS verification (AC10-AC14).
- **Content-Encoding defense.** Only `gzip` and `identity` accepted. Non-advertised encodings are rejected as protocol violations (AC4). Decompressed-byte cap enforced via manual zlib (AC3).
- **TLS hostname verification contractual.** The custom resolver preserves the original hostname for SNI/cert validation. An integration test (AC12) proves that IP-only certs are rejected.
- **PDF parser isolation.** `ProcessPoolExecutor(max_tasks_per_child=1)` ensures each PDF is validated in a fresh process, preventing cross-PDF state corruption (AC20). 30s timeout prevents hostile PDFs from hanging the pool (AC21).
- **Denial-of-crawl defense.** Transient failures do NOT mark orgs complete (AC23). An adversary cannot permanently opt out of crawling by inducing reproducible errors.
- **No new network exposure.** Client-only change; no listening sockets. Progress reporting is log-only (no Unix socket).
- **Connection limits (defense-in-depth).** Connector: `limit=500, limit_per_host=2` (AC40). Application: per-host semaphore + org cap. Queue sizes bounded (AC39).
- **Graceful shutdown prevents data corruption.** DB writer flushes before exit (AC34). Flock prevents concurrent instances (AC30). Resume is idempotent (AC25). Double-SIGINT force-exits as escape hatch (AC35).
- **Halt-dir permissions.** Validated at startup; refuse to start if world-writable (AC33).
- **Log hygiene.** Progress logs may contain URLs and EINs; log retention follows the project's existing rotation policy.
- **Sync crawler maintenance.** Once async is validated, sync enters bug-fix-only mode. Both share security-critical pure modules (`url_guard`, `redirect_policy`, `is_address_allowed`).

## Testing Strategy

### Unit Tests
1. Async HTTP client with mocked `aiohttp` responses (using `aioresponses` or manual `AsyncMock`).
2. Async throttle with mock `loop.time()`.
3. `AsyncHostPinCache` with mock `getaddrinfo` — positive pin, negative pin (private IP), multi-address selection (IPv4 preferred), negative caching.
4. Queue backpressure: verify producers block when queue is full.
5. `DBWriterActor`: batch accumulation, flush on threshold, flush on timeout, retry on failure, graceful stop.
6. `OrgDownloadTracker`: barrier fires when all decrements received.
7. Content-Encoding rejection: `br`, `deflate`, `zstd`, stacked encodings all return `blocked_content_type`.

### Integration Tests
8. Full async pipeline with deterministic stub fetcher. Verify candidate + PDF SHA256 parity with synchronous crawler output (AC26).
9. Multi-hop cross-host redirect chains — verify each hop triggers independent DNS resolution and SSRF check (AC14).
10. Oversized gzip response — verify decompressed-byte cap fires before full body is read.

### Security Tests
11. TLS hostname verification with custom resolver: cert-for-hostname passes, cert-for-IP-only fails (AC12).
12. DNS rebinding: hostname resolving to `127.0.0.1` is rejected and cached (AC11, AC13).
13. Denial-of-crawl: org with reproducible network errors is NOT marked complete; resume retries (AC23).
14. ProcessPool worker isolation: verify worker PID changes between consecutive PDF validations (AC20).
15. PDF validation timeout: hostile PDF that hangs parser is terminated after 30s (AC21).

### Operational Tests
16. SIGINT mid-crawl → verify DB writes flushed, no partial `crawled_orgs` for in-flight orgs.
17. Halt-file appearance → same graceful shutdown.
18. Double-SIGINT → force exit.
19. Resume after interruption → partial orgs retried, completed orgs skipped.

### Performance Benchmarks
20. 100-org set with real network. Capture wall-clock time (AC42).
21. 1000-org crawl, log peak RSS at shutdown (AC43).

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

- Phase 1 (async HTTP client): 300-400 lines
- Phase 2 (async throttle): 60-100 lines
- Phase 3 (async DNS pin cache): 80-120 lines
- Phase 4 (async discovery): 150-200 lines
- Phase 5 (async download): 100-140 lines
- Phase 6 (DB writer actor): 120-180 lines
- Phase 7 (orchestrator + shutdown + barriers): 300-400 lines
- Phase 8 (progress reporting): 60-100 lines
- Tests: 500-800 lines
- **Total**: ~1700-2400 lines of new code, ~30 lines modified in `crawler.py` (CLI flag)

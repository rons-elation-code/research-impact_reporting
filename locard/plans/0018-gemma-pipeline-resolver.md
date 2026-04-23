# Plan 0018 — Gemma Pipeline Resolver & Classifier

**Spec**: `locard/specs/0018-gemma-pipeline-resolver.md`  
**Date**: 2026-04-23

---

## Build Order

8 steps, ordered by dependency. Each step is independently testable.

---

### Step 1 — Brave Search client (`brave_search.py`)

**File**: `lavandula/nonprofits/brave_search.py` (NEW)

**What**: Standalone Brave Web Search client with domain blocklist filtering and global rate limiting.

```python
BLOCKLIST_DOMAINS: set[str]  # suffix-match set
BLOCKLIST_GOV_EXEMPT_WORDS = {"authority", "commission"}

class BraveRateLimiter:
    """Token-bucket rate limiter, thread-safe.
    Releases one permit per 1/qps seconds.
    Retries do NOT consume a new permit (AC25)."""
    def __init__(self, qps: float): ...
    def acquire(self) -> None: ...  # blocks until permit available

class BraveSearchResult:
    title: str
    url: str
    snippet: str

def is_blocked(domain: str, org_name: str) -> bool:
    """Suffix-match against BLOCKLIST_DOMAINS.
    *.gov exempt if org_name contains 'authority' or 'commission' (case-insensitive).
    linkedin-example.com does NOT match linkedin.com (AC19)."""

def search(query: str, *, api_key: str, count: int = 10,
           rate_limiter: BraveRateLimiter) -> list[BraveSearchResult]:
    """GET /res/v1/web/search. Retry 3x on 429/5xx with backoff.
    Retries reuse the rate limiter permit (AC25).
    Raises BraveSearchError on exhaustion."""

def search_and_filter(org_name: str, city: str, state: str, *,
                      api_key: str, rate_limiter: BraveRateLimiter,
                      max_results: int = 3) -> list[BraveSearchResult]:
    """Build query, search, filter blocklist, return top max_results."""
```

**Test file**: `lavandula/nonprofits/tests/unit/test_brave_search.py`

Tests (all mock HTTP, no live Brave):
- `test_search_returns_results` — mock 200 with 5 results, verify parsing
- `test_blocklist_suffix_match` — www.linkedin.com blocked, linkedin-example.com not (AC19)
- `test_blocklist_gov_exemption` — .gov blocked unless org name has "authority" (AC20)
- `test_rate_limiter_enforced` — 10 calls at QPS=2, verify wall time ≥ 4.5s (AC2)
- `test_retry_on_429` — mock 429 → 429 → 200, verify success after 3rd attempt
- `test_retry_does_not_double_count_rate_limit` — mock 429 → 200, verify only 1 permit consumed (AC25)
- `test_search_error_on_exhaustion` — mock 500 × 3, verify BraveSearchError raised
- `test_api_key_not_logged` — monkeypatch logging, verify key absent (AC28)
- `test_zero_results` — mock 200 with empty web.results, verify empty list returned

**ACs covered**: AC1, AC2, AC3, AC14, AC19, AC20, AC25, AC28

---

### Step 2 — Gemma client (`gemma_client.py`)

**File**: `lavandula/nonprofits/gemma_client.py` (NEW)

**What**: OpenAI-compatible client for Gemma 4 E4B. Two functions: disambiguate (URL resolution) and classify (report classification). Handles prompt construction, tool schemas, response parsing, and injection mitigations.

```python
RESOLUTION_TOOL = {
    "name": "record_resolution",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasoning": {"type": "string", "maxLength": 300},
        },
        "required": ["url", "confidence", "reasoning"],
    },
}

# Pinned from classify.py commit 842d613
CLASSIFIER_PROMPT_V1 = ...  # copy _SYSTEM_PROMPT
CLASSIFIER_TOOL_V1 = ...    # copy CLASSIFIER_TOOL

class GemmaClient:
    def __init__(self, *, base_url: str, model: str): ...

    def health_check(self) -> bool:
        """GET {base_url}/../api/tags, 5s timeout. Returns True if reachable."""

    def disambiguate(self, org: dict, candidates: list[dict]) -> dict:
        """Single LLM call. Returns {url, confidence, reasoning}.
        - Builds prompt with untrusted content tags (uuid per candidate)
        - Strips delimiter collisions from excerpts (AC22)
        - Enforces 12000 char total prompt cap
        - max_tokens=2000 (AC6)
        - temperature=0
        - response_format=json_object OR tool_choice forced (AC27)
        Raises GemmaParseError if response is malformed."""

    def classify(self, first_page_text: str) -> dict:
        """Single LLM call. Returns {classification, confidence, reasoning}.
        Uses CLASSIFIER_PROMPT_V1 + CLASSIFIER_TOOL_V1.
        Same transport settings as disambiguate."""
```

**Prompt construction for disambiguate**:
```python
def _build_candidates_block(candidates: list[dict]) -> str:
    parts = []
    for i, c in enumerate(candidates):
        tag_id = uuid4().hex
        excerpt = c.get("excerpt", "")[:3000]
        # AC22: strip delimiter collisions
        excerpt = excerpt.replace("</untrusted_web_content_", "[TAG_STRIPPED]")
        parts.append(
            f"[{i+1}] {c['final_url']}\n"
            f"<untrusted_web_content_{tag_id}>\n"
            f"{excerpt}\n"
            f"</untrusted_web_content_{tag_id}>"
        )
    block = "\n\n".join(parts)
    # AC: enforce 12000 char total
    # ... proportional truncation if needed
    return block
```

**Test file**: `lavandula/nonprofits/tests/unit/test_gemma_client.py`

Tests (mock HTTP, no live Ollama):
- `test_disambiguate_valid_response` — mock tool_use response, verify parsed dict (AC4)
- `test_classify_valid_response` — mock tool_use response, verify 5-enum (AC5)
- `test_max_tokens_is_2000` — assert constructed request body has max_tokens=2000 (AC6)
- `test_delimiter_collision_stripped` — excerpt with `</untrusted_web_content_` → `[TAG_STRIPPED]` (AC22)
- `test_prompt_size_capped_at_12000` — 3 candidates with 5000 char excerpts → proportionally truncated
- `test_health_check_reachable` — mock 200, returns True
- `test_health_check_unreachable` — mock timeout, returns False
- `test_parse_error_on_malformed` — mock non-JSON response, verify GemmaParseError
- `test_json_mode_or_tool_choice` — verify request includes response_format or tool_choice (AC27)
- `test_api_key_not_logged` — verify "ollama" key string doesn't leak (AC28, though trivial here)

**ACs covered**: AC4, AC5, AC6, AC15, AC22, AC27, AC28

---

### Step 3 — URL normalization utility

**File**: Add to `lavandula/nonprofits/brave_search.py` (or a small `url_utils.py`)

**What**: Normalize resolved URLs before DB write.

```python
def normalize_url(url: str) -> str:
    """
    - Strip tracking params: utm_*, fbclid, gclid, ref
    - Prefer HTTPS: if url is http://, try https:// HEAD; if 200, use https
    - Trailing slash: include for bare domains (https://example.org/),
      omit for paths (https://example.org/about)
    """
```

**Tests**:
- `test_strip_utm` — `https://foo.org/?utm_source=x&page=1` → `https://foo.org/?page=1`
- `test_strip_fbclid` — `https://foo.org/?fbclid=abc` → `https://foo.org/`
- `test_trailing_slash_bare_domain` — `https://foo.org` → `https://foo.org/`
- `test_no_trailing_slash_path` — `https://foo.org/about/` → `https://foo.org/about`
- `test_https_upgrade` — mock HEAD https → 200, input http → output https

---

### Step 4 — Pipeline queue (`pipeline_resolver.py` — queue only)

**File**: `lavandula/nonprofits/pipeline_resolver.py` (NEW, partial)

**What**: `PipelineQueue` class + SIGINT handler + shutdown semantics.

```python
import queue
import signal
import threading

_SENTINEL = None

class PipelineQueue:
    def __init__(self, maxsize: int = 32):
        self._q = queue.Queue(maxsize=maxsize)
        self._done = False
    
    def put(self, packet: dict, timeout: float = 60.0) -> None:
        self._q.put(packet, timeout=timeout)
    
    def get(self, timeout: float = 60.0) -> dict | None:
        item = self._q.get(timeout=timeout)
        return item  # None = sentinel
    
    def done(self) -> None:
        self._q.put(_SENTINEL)
    
    @property
    def qsize(self) -> int:
        return self._q.qsize()

class ShutdownFlag:
    """Cooperative shutdown. SIGINT sets this; producer checks before each org."""
    def __init__(self):
        self._event = threading.Event()
    def set(self): self._event.set()
    def is_set(self) -> bool: return self._event.is_set()

def install_sigint_handler(flag: ShutdownFlag) -> None:
    prev = signal.getsignal(signal.SIGINT)
    def handler(signum, frame):
        flag.set()
        # Restore default so second Ctrl-C kills hard
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGINT, handler)
```

**Tests**:
- `test_queue_put_get` — basic enqueue/dequeue
- `test_sentinel_terminates_consumer` — `done()` → `get()` returns None
- `test_backpressure` — full queue blocks put until consumer drains
- `test_qsize_tracks_depth` — verify qsize > 0 during fill
- `test_sigint_sets_flag` — `signal.raise_signal(SIGINT)` sets ShutdownFlag (AC23 partial)

**ACs covered**: AC7 (partial), AC23 (partial)

---

### Step 5 — Producer (Stages 1-4)

**File**: `lavandula/nonprofits/pipeline_resolver.py` (extend)

**What**: Producer function that runs Stages 1-4 in a thread, filling the queue.

```python
def producer(
    orgs: Iterable[dict],
    *,
    queue: PipelineQueue,
    api_key: str,
    rate_limiter: BraveRateLimiter,
    fetch_parallelism: int = 8,
    shutdown: ShutdownFlag,
) -> ProducerStats:
    """For each org:
    1. Brave search (via search_and_filter)
    2. If no results → write unresolved directly, continue
    3. HTTP fetch candidates (ThreadPoolExecutor, fetch_parallelism threads)
       - Per-thread ReportsHTTPClient (AC21)
    4. If no live candidates → write unresolved directly, continue
    5. Build candidate packet → queue.put()
    On shutdown.is_set(), stop after current org.
    Finally: queue.done()
    Returns ProducerStats(searched, enqueued, skipped_no_results, skipped_no_live).
    """
```

Fetch uses per-thread `ReportsHTTPClient` via `threading.local()`:
```python
_tls = threading.local()
def _get_http_client():
    if not hasattr(_tls, 'client'):
        _tls.client = ReportsHTTPClient(allow_insecure_cleartext=True)
    return _tls.client
```

**Tests**:
- `test_producer_enqueues_packets` — mock Brave + fetch, verify packets in queue
- `test_producer_skips_no_results` — mock empty Brave, verify unresolved written directly
- `test_producer_skips_no_live` — mock Brave with results but all fetch fail, verify unresolved
- `test_producer_shutdown_stops_early` — set ShutdownFlag after 3 orgs, verify queue.done() called
- `test_fetch_per_thread_client` — verify ReportsHTTPClient constructed per thread (AC21)
- `test_ssrf_blocked_after_redirect` — mock redirect to 169.254.169.254, verify blocked (AC26)

**ACs covered**: AC3, AC7, AC21, AC23, AC26

---

### Step 6 — Consumer (Stages 5-6)

**File**: `lavandula/nonprofits/pipeline_resolver.py` (extend)

**What**: Consumer function that pulls from the queue, calls Gemma, writes to RDS.

```python
def consumer(
    *,
    queue: PipelineQueue,
    gemma: GemmaClient,
    engine: Engine,
    shutdown: ShutdownFlag,
) -> ConsumerStats:
    """Loop:
    1. packet = queue.get() → if None, break (producer done)
    2. result = gemma.disambiguate(packet org, packet candidates)
       - On ConnectionError: retry 3x (5/10/20s). On exhaustion → unresolved, inference_unavailable (AC11)
       - On GemmaParseError → unresolved, llm_parse_error
    3. Apply confidence thresholds → resolved/ambiguous/unresolved
    4. Normalize URL (Step 3)
    5. Write to RDS (single UPDATE, commit) (AC8)
       - On DB error: log write_error, continue (AC24)
    Returns ConsumerStats(resolved, unresolved, ambiguous, errors).
    """
```

**Tests**:
- `test_consumer_resolves_high_confidence` — mock Gemma returns 0.9 → resolver_status=resolved
- `test_consumer_ambiguous` — mock Gemma returns two candidates ≥ 0.6, within 0.1 → ambiguous
- `test_consumer_unresolved_low_confidence` — mock Gemma returns 0.5 → unresolved
- `test_consumer_retry_on_connection_error` — mock 3 ConnectionErrors then success (AC11)
- `test_consumer_inference_unavailable` — mock 3 ConnectionErrors, no recovery → unresolved (AC11)
- `test_consumer_parse_error` — mock malformed Gemma response → unresolved, llm_parse_error
- `test_consumer_db_write_failure` — mock DB exception → logged, consumer continues (AC24)
- `test_consumer_per_org_commit` — verify commit after each org (AC8)
- `test_consumer_stops_on_sentinel` — queue with sentinel → consumer exits

**ACs covered**: AC8, AC11, AC16, AC24

---

### Step 7 — CLI entry points

**Files**: 
- `lavandula/nonprofits/tools/pipeline_resolve.py` (NEW)
- `lavandula/nonprofits/tools/pipeline_classify.py` (NEW)

**What**: argparse CLIs that wire everything together.

`pipeline_resolve.py`:
```python
def main():
    args = parse_args()
    # Health check Gemma endpoint (AC: exits if unreachable)
    gemma = GemmaClient(base_url=args.gemma_url, model=args.gemma_model)
    if not gemma.health_check():
        print("ERROR: Gemma endpoint unreachable at", args.gemma_url, file=sys.stderr)
        sys.exit(1)
    
    api_key = get_brave_api_key()
    engine = make_app_engine()
    rate_limiter = BraveRateLimiter(args.brave_qps)
    pq = PipelineQueue(maxsize=args.queue_size)
    shutdown = ShutdownFlag()
    install_sigint_handler(shutdown)
    
    # Load orgs from RDS
    orgs = load_unresolved_orgs(engine, state=args.state, limit=args.limit,
                                 status_filter=args.status_filter)
    
    if args.dry_run:
        # Run producer only, print candidate packets to stdout
        run_dry(orgs, api_key=api_key, rate_limiter=rate_limiter,
                fetch_parallelism=args.fetch_parallelism)
        return
    
    # Start producer in background thread
    producer_thread = threading.Thread(
        target=producer, kwargs={...}, daemon=True)
    producer_thread.start()
    
    # Consumer in main thread
    stats = consumer(queue=pq, gemma=gemma, engine=engine, shutdown=shutdown)
    producer_thread.join(timeout=10)
    
    # Print summary (AC18)
    print_summary(stats, wall_time, brave_queries)
```

`pipeline_classify.py`: Same pattern, different producer (keyset pagination over `reports` where `classification IS NULL`), consumer calls `gemma.classify()`.

**Tests**:
- `test_dry_run_no_gemma_call` — verify Gemma never called in dry-run (AC10)
- `test_resume_skips_resolved` — pre-populate DB with resolved org, verify skipped (AC9)
- `test_summary_printed` — capture stdout, verify counts present (AC18)
- `test_exits_if_gemma_unreachable` — mock health_check False → SystemExit

**ACs covered**: AC9, AC10, AC18

---

### Step 8 — Integration test

**File**: `lavandula/nonprofits/tests/integration/test_pipeline_live.py` (NEW)

**What**: Behind `LAVANDULA_LIVE_GEMMA=1`. Runs the full pipeline against live Brave + live Gemma on 10 TX unresolved orgs.

```python
@pytest.mark.skipunless(os.getenv("LAVANDULA_LIVE_GEMMA") == "1",
                        "requires live Gemma + Brave")
class TestPipelineLive:
    def test_resolve_tx_10(self):
        """AC12: ≥ 8/10 resolved on TX unresolved orgs."""
        ...
    
    def test_classify_10_reports(self):
        """AC13: ≥ 8/10 match existing Haiku classifications."""
        ...
```

**ACs covered**: AC12, AC13

---

## AC Coverage Matrix

| AC | Step | Test |
|----|------|------|
| AC1 | 1 | test_search_returns_results |
| AC2 | 1 | test_rate_limiter_enforced |
| AC3 | 1,5 | test_zero_results, test_producer_skips_no_results |
| AC4 | 2 | test_disambiguate_valid_response |
| AC5 | 2 | test_classify_valid_response |
| AC6 | 2 | test_max_tokens_is_2000 |
| AC7 | 4,5 | test_qsize_tracks_depth, test_producer_enqueues_packets |
| AC8 | 6 | test_consumer_per_org_commit |
| AC9 | 7 | test_resume_skips_resolved |
| AC10 | 7 | test_dry_run_no_gemma_call |
| AC11 | 6 | test_consumer_retry_on_connection_error, test_consumer_inference_unavailable |
| AC12 | 8 | test_resolve_tx_10 (manual) |
| AC13 | 8 | test_classify_10_reports (manual) |
| AC14 | 1 | test_api_key_not_logged |
| AC15 | 2 | test_disambiguate_valid_response (verifies tag wrapping) |
| AC16 | 6 | test_consumer_resolves_high_confidence (verifies method column) |
| AC17 | 1,2,8 | all unit tests mock; integration behind flag |
| AC18 | 7 | test_summary_printed |
| AC19 | 1 | test_blocklist_suffix_match |
| AC20 | 1 | test_blocklist_gov_exemption |
| AC21 | 5 | test_fetch_per_thread_client |
| AC22 | 2 | test_delimiter_collision_stripped |
| AC23 | 4,7 | test_sigint_sets_flag, test_pipeline_sigint (end-to-end) |
| AC24 | 6 | test_consumer_db_write_failure |
| AC25 | 1 | test_retry_does_not_double_count_rate_limit |
| AC26 | 5 | test_ssrf_blocked_after_redirect |
| AC27 | 2 | test_json_mode_or_tool_choice |
| AC28 | 1,2 | test_api_key_not_logged |

---

## Traps to Avoid

1. **Don't use `time.sleep()` for rate limiting.** Use a token-bucket or semaphore-based approach so the rate limiter is testable without wall-clock waits.

2. **Don't construct `ReportsHTTPClient` in the producer thread and pass it to fetch threads.** The client must be per-thread (`threading.local()`). The existing codebase uses this pattern (TICK-002).

3. **Don't import `classify.py` at runtime for the prompt.** The spec requires pinning to commit 842d613. Copy the constants into `gemma_client.py` so they don't drift.

4. **Don't use `queue.Queue.join()` for shutdown.** It blocks indefinitely if the consumer crashes. Use the sentinel pattern (`None`) instead.

5. **Don't retry Brave searches on 400 (bad request).** Only retry on 429 (rate limit) and 5xx (server error). 400 means the query is malformed — retrying won't help.

6. **Don't forget to handle the case where Gemma supports tool_choice but not response_format=json_object.** Try JSON mode first; if the endpoint returns 400, fall back to forced tool_choice. Cache the capability for the rest of the run.

7. **Don't write `resolver_status=unresolved` in the producer thread AND the consumer thread for the same org.** Producer writes unresolved only for orgs that skip the queue (no results/no live). Consumer writes for orgs that went through Gemma. If an org enters the queue, only the consumer writes.

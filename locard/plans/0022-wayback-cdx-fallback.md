# Plan 0022 — Wayback Machine CDX Fallback for Cloudflare-blocked Sites

**Spec**: `locard/specs/0022-wayback-cdx-fallback.md`
**Created**: 2026-04-25

---

## Consultation Log

(To be populated during plan-review and red-team rounds.)

---

## Implementation Order

7 phases, bottom-up. Each phase is independently testable. Phases 1-3 are foundational primitives; 4-5 build the Wayback discovery; 6 wires it into the orchestrator; 7 ships migration + tests.

### Phase 1: Domain & URL Validation Helpers (`wayback_validation.py`)

**New file**: `lavandula/reports/wayback_validation.py` (~120 lines)

**Why first**: Covers the two CRITICAL injection paths from the red-team review. Pure functions, easy to test exhaustively, no I/O. Other phases depend on these as primitives.

**What to build:**

```python
import re
from urllib.parse import urlsplit
from typing import Optional

# RFC 1123 hostname regex: LDH characters and dots, max 253 chars total.
# AC15.2.
_HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_HOSTNAME_MAX_LEN = 253
_TIMESTAMP_RE = re.compile(r"^\d{14}$")
_ORIGINAL_MAX_LEN = 2048

def validate_domain(domain: str) -> Optional[str]:
    """Return the lowercased domain if valid per AC15.2, else None.
    
    Strict RFC-1123 hostname format. Rejects anything containing
    URL-special characters that could smuggle CDX query parameters.
    """
    if not domain or len(domain) > _HOSTNAME_MAX_LEN:
        return None
    domain = domain.lower()
    if not _HOSTNAME_RE.fullmatch(domain):
        return None
    return domain


def validate_cdx_row(row: list) -> Optional[dict]:
    """Validate a CDX response row per AC15.3. Return a normalized dict
    or None if the row should be skipped.
    
    Expected schema: [urlkey, timestamp, original, mimetype, statuscode, digest, length]
    Defensive: extra columns ignored, short rows skipped.
    """
    if not isinstance(row, list) or len(row) < 3:
        return None
    urlkey, timestamp, original = row[0], row[1], row[2]
    digest = row[5] if len(row) > 5 else None

    if not isinstance(timestamp, str) or not _TIMESTAMP_RE.fullmatch(timestamp):
        return None
    if not isinstance(original, str) or len(original) > _ORIGINAL_MAX_LEN:
        return None

    try:
        parsed = urlsplit(original)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.hostname:
        return None
    if not validate_domain(parsed.hostname):
        return None

    # Strip credentials and fragment.
    cleaned = _strip_credentials_and_fragment(original, parsed)
    if cleaned is None:
        return None

    return {
        "urlkey": urlkey,
        "timestamp": timestamp,
        "original": cleaned,
        "capture_host": parsed.hostname.lower(),
        "digest": digest,
    }


def _strip_credentials_and_fragment(original: str, parsed) -> Optional[str]:
    """Reconstruct the URL without credentials or fragment. Returns
    None if reconstruction fails."""
    # Build host[:port] from parsed.hostname and parsed.port (drops user:pass)
    netloc = parsed.hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{netloc}{path}{query}"


def build_wayback_url(timestamp: str, original: str) -> str:
    """Construct the id_-modifier raw-bytes Wayback URL per AC8.
    
    Caller MUST validate timestamp and original via validate_cdx_row first.
    """
    from urllib.parse import quote
    safe_chars = ":/?#[]@!$&'()*+,;="
    return f"https://web.archive.org/web/{timestamp}id_/{quote(original, safe=safe_chars)}"
```

**Tests** (`test_wayback_validation.py`, ~150 lines):
- `validate_domain` accepts `sloan.org`, `reports.sloan.org`, valid IDN-encoded hosts; rejects `evil.org&matchType=exact`, `evil.org/`, `evil.org?x=y`, `..`, empty string, oversized strings, control chars (AC25.2).
- `validate_cdx_row` accepts well-formed rows; rejects timestamp `"../../etc"`, `"2024"` (wrong length), original with `javascript:` scheme, header-injection (`http://x.com\r\nHost:evil`), oversized original, missing host, schema-drift rows (AC25.3).
- `validate_cdx_row` strips embedded credentials and fragments.
- `build_wayback_url` properly escapes special chars in `original` (AC25.3).

**ACs covered**: AC15.2, AC15.3, AC25.2, AC25.3 (test infrastructure)

**Lines**: ~120 src + ~150 tests

---

### Phase 2: Wayback Throttle Override (`async_host_throttle.py` modification)

**Modify**: `lavandula/reports/async_host_throttle.py` (~30 lines added)

**What to change:**

`AsyncHostThrottle.__init__` accepts an optional `host_overrides: dict[str, float]` mapping a normalized host key to its per-request delay. Default delay (`min_interval_sec`) applies to hosts not in the map.

`_get_semaphore` and the sleep calculation use the per-host delay if the canonicalized host matches an entry in `host_overrides`, otherwise fall back to `min_interval_sec`.

A new helper `_normalize_wayback_host(host: str) -> str` returns `"archive.org"` for any of `web.archive.org`, `archive.org`, `*.archive.org`, ensuring the two share a single throttle bucket per AC17.3.

**Implementation skeleton:**

```python
def _canonical_host(host: str) -> str:
    h = host.lower().strip()
    # AC17.3: web.archive.org and archive.org share one bucket.
    if h == "web.archive.org" or h.endswith(".archive.org") or h == "archive.org":
        return "archive.org"
    return h


class AsyncHostThrottle:
    def __init__(
        self,
        *,
        min_interval_sec: float | None = None,
        jitter_sec: float | None = None,
        host_overrides: dict[str, float] | None = None,
    ) -> None:
        ...
        self._host_overrides = host_overrides or {}

    def _interval_for(self, host: str) -> float:
        return self._host_overrides.get(_canonical_host(host), self._min_interval)

    @asynccontextmanager
    async def request(self, host: str) -> AsyncIterator[None]:
        canonical = _canonical_host(host)
        sem = await self._get_semaphore(canonical)  # changed: use canonical key
        await sem.acquire()
        try:
            interval = self._interval_for(host)
            ...
```

**Config addition** (`lavandula/reports/config.py`):

```python
# Spec 0022: Wayback throttle. 0.25s = 4 req/sec (well under Wayback's
# documented ~15 req/sec limit but leaves comfortable headroom).
WAYBACK_REQUEST_DELAY_SEC = 0.25

# Spec 0022: cap on PDF candidates per Wayback recovery.
WAYBACK_MAX_PDFS_PER_ORG = 30

# Spec 0022: cap on distinct subdomains contributing to a single org's
# recovery. Bounds blast radius of subdomain takeover (AC15.4).
WAYBACK_MAX_DISTINCT_SUBDOMAINS = 3

# Spec 0022: CDX response body cap (AC15.5). Reuses MAX_TEXT_BYTES.
# (No new constant needed.)
```

**Constructor caller update** (`async_crawler.py:run_async`):

```python
throttle = AsyncHostThrottle(host_overrides={
    "archive.org": config.WAYBACK_REQUEST_DELAY_SEC,
})
```

**Tests** (modify existing `test_async_host_throttle.py`, ~40 lines added):
- Override applied for `web.archive.org` and `archive.org` (single bucket).
- Default `min_interval_sec` applied for other hosts.
- Two consecutive requests to `web.archive.org` and `archive.org` serialize through the SAME semaphore (AC17.3 — verify only one in flight at a time).

**ACs covered**: AC10, AC17.3

**Lines**: ~30 src + ~40 tests

---

### Phase 3: Retry-After Header Honor (`async_http_client.py` modification)

**Modify**: `lavandula/reports/async_http_client.py` (~30 lines added)

**What to change:**

In the `get` method, after receiving any response with status 429 or 503, check for a `Retry-After` header. If present and parseable (numeric seconds, capped at 60), record it on the FetchResult OR sleep before returning. The simplest implementation: if the header is present, sleep for the indicated seconds (capped) BEFORE returning the response, and return the FetchResult unchanged. This naturally back-pressures any subsequent request to the same host.

Alternative (cleaner but bigger): expose `retry_after_sec` on `FetchResult` so the caller decides whether to honor it. For Spec 0022's purposes, the simpler in-client sleep is sufficient — it only affects 429/503 responses, which are already considered "failed" by the discover_org logic, so adding a sleep doesn't hurt the happy path.

**Implementation:**

```python
async def _maybe_honor_retry_after(self, resp) -> None:
    """If response is 429/503 with Retry-After header, sleep up to 60s."""
    if resp.status not in (429, 503):
        return
    retry_after = resp.headers.get("Retry-After")
    if not retry_after:
        return
    try:
        delay = float(retry_after)
    except (TypeError, ValueError):
        return
    delay = min(max(delay, 0.0), 60.0)
    if delay > 0:
        _log.info("honoring Retry-After=%s for host=%s", retry_after, resp.host)
        await asyncio.sleep(delay)
```

Called inside `get()` after the response is received and before returning.

**Tests** (modify `test_async_http_client.py`, ~30 lines added):
- 429 with `Retry-After: 5` → asyncio.sleep called with ~5s (use mocked sleep).
- 429 without `Retry-After` → no sleep.
- 200 with `Retry-After` → no sleep (only honored on 429/503).
- `Retry-After: bogus` → no sleep, no exception.
- `Retry-After: 1000` → capped to 60s.

**ACs covered**: AC17.2, AC25.7

**Lines**: ~30 src + ~30 tests

---

### Phase 4: Wayback Discovery Module (`wayback_fallback.py`)

**New file**: `lavandula/reports/wayback_fallback.py` (~250 lines)

**What to build:**

```python
from enum import Enum

class WaybackOutcome(str, Enum):
    RECOVERED = "recovered"
    EMPTY     = "empty"
    ERROR     = "error"

@dataclass
class WaybackResult:
    outcome: WaybackOutcome
    candidates: list[Candidate]
    capture_hosts: list[str]            # for decisions_log
    raw_row_count: int                  # how many rows CDX returned (pre-dedup)
    elapsed_ms: int

async def discover_via_wayback(
    *,
    seed_url: str,
    seed_etld1: str,
    client: AsyncHTTPClient,
    ein: str,
) -> WaybackResult:
    """Query Wayback CDX for PDFs under the domain, validate rows,
    enforce capture-host policy, return candidates pointing at
    web.archive.org raw-bytes URLs.
    """
    ...
```

**Internal helpers:**

```python
def _build_cdx_url(domain: str) -> str | None:
    """Build the CDX query URL with strict domain validation (AC15.2)."""
    validated = validate_domain(domain)
    if validated is None:
        return None
    encoded = urllib.parse.quote(validated, safe="")
    return (
        f"https://web.archive.org/cdx/search/cdx?"
        f"url={encoded}/*&"
        f"matchType=domain&"
        f"filter=mimetype:application/pdf&"
        f"filter=statuscode:200&"
        f"output=json&"
        f"limit=500"
    )


def _parse_cdx_response(body: bytes) -> tuple[WaybackOutcome, list[dict]]:
    """Parse CDX JSON. Returns (outcome, validated_rows).
    
    AC15.3: row-level validation via validate_cdx_row.
    State machine per spec section 'State machine: outcome classification':
    - JSON parse failure → ERROR
    - Empty list or header-only → EMPTY
    - All rows fail validation → EMPTY (no usable coverage)
    - Some rows valid → RECOVERED
    """
    try:
        rows = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (WaybackOutcome.ERROR, [])
    if not isinstance(rows, list) or len(rows) < 1:
        return (WaybackOutcome.EMPTY, [])
    # First row is header; skip and validate the rest.
    validated = []
    for row in rows[1:]:
        v = validate_cdx_row(row)
        if v is not None:
            validated.append(v)
    if not validated:
        return (WaybackOutcome.EMPTY, [])
    return (WaybackOutcome.RECOVERED, validated)


def _dedupe_and_cap(
    rows: list[dict],
    seed_etld1: str,
    max_pdfs: int,
    max_subdomains: int,
) -> tuple[list[dict], list[str]]:
    """Apply per-spec post-validation policy:
    
    1. Group by urlkey, pick max-timestamp per group.
    2. Sort by timestamp DESC.
    3. Filter by capture-host eTLD+1 == seed_etld1 (AC15.4).
    4. Cap distinct capture hosts at max_subdomains, with apex preference.
    5. Cap total at max_pdfs.
    
    Returns (deduped rows, distinct capture hosts kept).
    """
    # Step 1+2: dedup by urlkey
    by_urlkey: dict[str, dict] = {}
    for r in rows:
        prev = by_urlkey.get(r["urlkey"])
        if prev is None or r["timestamp"] > prev["timestamp"]:
            by_urlkey[r["urlkey"]] = r
    candidates = sorted(
        by_urlkey.values(), key=lambda r: r["timestamp"], reverse=True,
    )

    # Step 3: filter by eTLD+1 ownership
    filtered = []
    for r in candidates:
        if etld1(r["capture_host"]) == seed_etld1:
            filtered.append(r)

    # Step 4: cap distinct subdomains, apex required if present
    apex = seed_etld1
    apex_candidates = [r for r in filtered if r["capture_host"] == apex]
    other_candidates = [r for r in filtered if r["capture_host"] != apex]
    subdomain_quota = max_subdomains - (1 if apex_candidates else 0)
    
    distinct_other_hosts: list[str] = []
    kept_other: list[dict] = []
    for r in other_candidates:
        host = r["capture_host"]
        if host in distinct_other_hosts:
            kept_other.append(r)
        elif len(distinct_other_hosts) < subdomain_quota:
            distinct_other_hosts.append(host)
            kept_other.append(r)
    
    final = (apex_candidates + kept_other)[:max_pdfs]
    capture_hosts = sorted({r["capture_host"] for r in final})
    return (final, capture_hosts)


def _row_to_candidate(row: dict, seed_url: str) -> Candidate:
    """Build a Candidate with Wayback attribution per AC11."""
    wayback_url = build_wayback_url(row["timestamp"], row["original"])
    return Candidate(
        url=wayback_url,
        referring_page_url=seed_url,
        anchor_text=row["original"],
        discovered_via="wayback",
        hosting_platform="wayback",
        attribution_confidence="wayback_archive",
        original_source_url=row["original"],   # new field
        wayback_digest=row.get("digest"),       # for fetch_log
    )
```

**Top-level orchestration in `discover_via_wayback`:**

```python
async def discover_via_wayback(*, seed_url, seed_etld1, client, ein) -> WaybackResult:
    t_start = asyncio.get_event_loop().time()
    domain = urlsplit(seed_url).hostname or seed_etld1
    cdx_url = _build_cdx_url(domain)
    if cdx_url is None:
        # Domain failed validation — AC15.2 / AC25.2.
        return WaybackResult(WaybackOutcome.ERROR, [], [], 0, 0)
    
    r = await client.get(cdx_url, kind="wayback-cdx")
    elapsed = int((asyncio.get_event_loop().time() - t_start) * 1000)
    
    if r.status != "ok" or not r.body:
        return WaybackResult(WaybackOutcome.ERROR, [], [], 0, elapsed)
    
    # AC15.5: body cap (existing http_client enforces MAX_TEXT_BYTES already)
    
    outcome, validated = _parse_cdx_response(r.body)
    if outcome != WaybackOutcome.RECOVERED:
        return WaybackResult(outcome, [], [], 0, elapsed)
    
    deduped, capture_hosts = _dedupe_and_cap(
        validated,
        seed_etld1=seed_etld1,
        max_pdfs=config.WAYBACK_MAX_PDFS_PER_ORG,
        max_subdomains=config.WAYBACK_MAX_DISTINCT_SUBDOMAINS,
    )
    if not deduped:
        # All rows filtered out by ownership policy.
        return WaybackResult(
            WaybackOutcome.EMPTY, [], [], len(validated), elapsed,
        )
    
    candidates = [_row_to_candidate(r, seed_url) for r in deduped]
    return WaybackResult(
        WaybackOutcome.RECOVERED, candidates, capture_hosts,
        len(validated), elapsed,
    )
```

**Tests** (`test_wayback_fallback.py`, ~300 lines):
- `_build_cdx_url`: malicious domain rejected, valid domain produces correctly-encoded URL (AC25.2).
- `_parse_cdx_response`: empty list → EMPTY; header-only → EMPTY; all-rows-fail-validation → EMPTY; valid rows → RECOVERED with the validated rows; non-JSON body → ERROR; UnicodeDecodeError → ERROR (AC20, AC20.1, AC24.4, AC24.5).
- `_dedupe_and_cap`:
  - dedup by urlkey picks max timestamp (AC20)
  - sort by timestamp DESC
  - cross-eTLD+1 rows dropped (AC25.4)
  - max 3 subdomains with apex preferred (AC25.4)
  - cap at WAYBACK_MAX_PDFS_PER_ORG (AC7)
- `discover_via_wayback`: full flow with stubbed AsyncHTTPClient — RECOVERED, EMPTY, ERROR, capture_hosts populated, elapsed_ms recorded (AC23, AC24).

**ACs covered**: AC4, AC5, AC5.1, AC5.2, AC7, AC11, AC15.2-AC15.4, AC20, AC20.1, AC24.4, AC24.5, AC25.4

**Lines**: ~250 src + ~300 tests

---

### Phase 5: Async Discover Integration (`async_discover.py` modification)

**Modify**: `lavandula/reports/async_discover.py` (~50 lines added)

**What to change:**

After the existing direct discovery completes, check the AC1 condition:
```python
should_fall_back = (
    not result.candidates
    and not result.homepage_ok
    and not result.robots_disallowed_all
)
```

If true, call `discover_via_wayback()` and merge the results into `DiscoveryResult`.

`DiscoveryResult` gains a `wayback_outcome: WaybackOutcome | None = None` field (None if Wayback wasn't queried) and `wayback_capture_hosts: list[str] = []` for decisions_log forensics.

The `_org_worker`'s outcome handling (Phase 6) reads these fields and routes to the appropriate `crawled_orgs.status` per AC14's 5-way model.

**Implementation:**

```python
@dataclass
class DiscoveryResult:
    candidates: list[Candidate] = field(default_factory=list)
    homepage_ok: bool = False
    robots_disallowed_all: bool = False
    homepage_failure_reason: str | None = None  # AC3.1
    wayback_outcome: WaybackOutcome | None = None
    wayback_capture_hosts: list[str] = field(default_factory=list)
    wayback_raw_row_count: int = 0
    wayback_elapsed_ms: int = 0


async def discover_org(*, seed_url, seed_etld1, client, robots_text, ein, fetcher=None) -> DiscoveryResult:
    result = ... # existing direct discovery, fills candidates / homepage_ok / robots_disallowed_all
    
    # Compute homepage failure reason for AC3.1
    if not result.homepage_ok:
        result.homepage_failure_reason = _classify_homepage_failure(home_status, home_resp)
    
    # AC1 gate: try Wayback if direct discovery yielded nothing AND robots didn't block
    if not result.candidates and not result.homepage_ok and not result.robots_disallowed_all:
        wayback = await discover_via_wayback(
            seed_url=seed_url,
            seed_etld1=seed_etld1,
            client=client,
            ein=ein,
        )
        result.wayback_outcome = wayback.outcome
        result.wayback_capture_hosts = wayback.capture_hosts
        result.wayback_raw_row_count = wayback.raw_row_count
        result.wayback_elapsed_ms = wayback.elapsed_ms
        if wayback.outcome == WaybackOutcome.RECOVERED:
            result.candidates = wayback.candidates  # downstream pipeline treats them like normal candidates
    
    return result


def _classify_homepage_failure(status: str, resp: Optional[FetchResult]) -> str:
    """AC3.1: bounded enum of failure reasons."""
    if resp and resp.http_status == 403:
        cf_server = (resp.headers.get("server") or "").lower()
        cf_mitigated = resp.headers.get("cf-mitigated")
        if "cloudflare" in cf_server or cf_mitigated:
            return "homepage_cloudflare_challenge"
    if resp and resp.http_status:
        if 400 <= resp.http_status < 500:
            return "homepage_4xx"
        if 500 <= resp.http_status < 600:
            return "homepage_5xx"
    if status == "network_error":
        return "homepage_network_error"
    if status == "size_capped":
        return "homepage_size_capped"
    if status == "blocked_content_type":
        return "homepage_blocked_content_type"
    return "homepage_unknown"
```

**Tests** (modify `test_async_discover.py`, ~80 lines added):
- AC1: Wayback fires only when all three conditions hold (test each combination).
- AC2: Wayback does NOT fire when direct found 1+ candidates (even if homepage 403'd).
- AC3: Wayback does NOT fire when robots blocked everything.
- AC3.1: classification correct for 403-Cloudflare, 403-other, 5xx, network_error, size_capped, blocked_content_type.

**ACs covered**: AC1, AC2, AC3, AC3.1, AC21

**Lines**: ~50 src + ~80 tests

---

### Phase 6: Crawler Orchestration & DB State Machine (`async_crawler.py` modification)

**Modify**: `lavandula/reports/async_crawler.py` (~70 lines added)

**What to change:**

`_process_org_async` reads the `DiscoveryResult.wayback_outcome` and routes the org's `crawled_orgs.status` per AC14's 5-way model. The transient-discovery branch (added in Spec 0021) is rewritten to consult Wayback's outcome:

```python
async def _process_org_async(...):
    discovery = await discover_org(..., fetcher=_fetcher_with_retry)
    
    if not discovery.candidates:
        # No candidates from any source. Decide DB state based on Wayback outcome.
        await _record_no_candidates(
            ein=ein,
            db_actor=db_actor,
            stats=stats,
            discovery=discovery,
        )
        return
    
    # ... existing happy-path processing of discovery.candidates
    # (Wayback candidates flow through the same download workers)
```

```python
async def _record_no_candidates(*, ein, db_actor, stats, discovery):
    """AC14 5-way state machine for orgs that produced no candidates."""
    if discovery.wayback_outcome is None:
        # Wayback wasn't queried (e.g., robots blocked) — treat as standard transient.
        stats.orgs_transient_failed += 1
        await _enqueue_status(db_actor, ein, "transient", "robots_or_unknown")
        return
    
    if discovery.wayback_outcome == WaybackOutcome.ERROR:
        stats.orgs_transient_failed += 1
        stats.wayback_errors += 1
        await _enqueue_status(db_actor, ein, "transient", "wayback_error")
        return
    
    if discovery.wayback_outcome == WaybackOutcome.EMPTY:
        # AC15.6: two-strikes empty rule.
        # First empty → transient with notes='wayback_no_coverage'.
        # Second consecutive empty → permanent_skip (handled by SQL CASE
        # via existing notes inspection).
        stats.wayback_empty += 1
        stats.orgs_transient_failed += 1  # counts toward transient until promoted
        await _enqueue_status_with_two_strikes(
            db_actor, ein, status="transient", notes="wayback_no_coverage",
        )
        return


async def _enqueue_status(db_actor, ein, status, notes):
    await db_actor.enqueue(UpsertCrawledOrgRequest(
        ein=ein,
        candidate_count=0,
        fetched_count=0,
        confirmed_report_count=0,
        status=status,
        notes=notes,
    ))


async def _enqueue_status_with_two_strikes(db_actor, ein, status, notes):
    """Enqueue a row that the SQL CASE will promote to permanent_skip
    on the second consecutive 'wayback_no_coverage' observation."""
    await db_actor.enqueue(UpsertCrawledOrgRequest(
        ein=ein,
        candidate_count=0,
        fetched_count=0,
        confirmed_report_count=0,
        status=status,
        notes=notes,
        two_strikes_check=True,  # tells the SQL CASE to consult prior notes
    ))
```

**SQL CASE update** (extend Spec 0021's upsert_crawled_org SQL, AC14):

```sql
ON CONFLICT (ein) DO UPDATE SET
  last_crawled_at = EXCLUDED.last_crawled_at,
  ...,
  attempts = crawled_orgs.attempts + 1,
  status = CASE
    WHEN EXCLUDED.status = 'ok' THEN 'ok'
    WHEN EXCLUDED.status = 'permanent_skip' THEN 'permanent_skip'
    -- Two-strikes empty (AC15.6, AC25.6): promote on second consecutive
    -- 'wayback_no_coverage' observation.
    WHEN EXCLUDED.status = 'transient'
         AND EXCLUDED.notes = 'wayback_no_coverage'
         AND crawled_orgs.notes = 'wayback_no_coverage'
         THEN 'permanent_skip'
    -- Existing attempts cap (Spec 0021).
    WHEN crawled_orgs.attempts + 1 >= :max_attempts
         THEN 'permanent_skip'
    ELSE EXCLUDED.status
  END,
  notes = EXCLUDED.notes
```

The `notes` column is already in `crawled_orgs` after migration 005 (added in Phase 7).

`CrawlStats` gains:
```python
wayback_attempts: int = 0
wayback_recoveries: int = 0
wayback_empty: int = 0
wayback_errors: int = 0
```

`_progress_reporter` adds these to the periodic log.

**Active-content rejection for Wayback PDFs (AC17.1)**: when processing a download in `_process_download`, if the candidate is `discovered_via='wayback'` AND the active-content scan finds JS/launch/URI actions, reject before archive PUT:

```python
flags = scan_active_content(outcome.body)
if cand.discovered_via == "wayback" and (
    flags["pdf_has_javascript"] or
    flags["pdf_has_launch"] or
    flags["pdf_has_uri_actions"]
):
    await db_actor.enqueue(RecordFetchRequest(
        ein=ein,
        url_redacted=outcome.final_url_redacted or redact_url(cand.url),
        kind="pdf-get",
        fetch_status="blocked_content_type",
        notes=sanitize("wayback_active_content"),
    ))
    return
```

**Tests** (new tests in `test_async_crawler.py`, ~150 lines added):
- AC14: 5-way DB outcome — recovered → `ok`; error → `transient` `wayback_error`; first empty → `transient` `wayback_no_coverage`; second empty (existing row already 'wayback_no_coverage') → `permanent_skip`; all-downloads-failed → `transient` `wayback_all_downloads_failed`.
- AC17.1: Wayback PDF with embedded JS rejected; archive.put NOT called; `reports` row NOT created; fetch_log row created with notes='wayback_active_content'.
- AC25.5: same as AC17.1 in test form.
- AC25.6: two consecutive empty CDX runs auto-promote to permanent_skip.
- AC23: integration — homepage 403 cf-mitigated, CDX returns 3 PDFs, all 3 archived, status=ok, wayback_recoveries=1.

**ACs covered**: AC14, AC17.1, AC18, AC23, AC24.1, AC24.2, AC24.3, AC24.6, AC25.5, AC25.6

**Lines**: ~70 src + ~150 tests

---

### Phase 7: Migration 005 + Schema & Static-Analysis Tests

**New file**: `lavandula/migrations/rds/005_wayback_provenance.sql`

```sql
-- Migration: 005_wayback_provenance
-- Date: 2026-04-25
-- Target: PostgreSQL (RDS lava_prod1), schema lava_impact
-- Adds:
--   reports.original_source_url_redacted TEXT NULL  (AC13)
--   crawled_orgs.notes TEXT NULL                   (AC14, two-strikes empty rule)
--
-- attribution_confidence is currently free-text (no CHECK constraint),
-- so adding 'wayback_archive' value requires no schema change.
-- Known values inventory (informational, not enforced):
--   'high', 'medium', 'low', 'platform_verified', 'own_domain',
--   'wayback_archive' (new in spec 0022).

BEGIN;
SET search_path TO lava_impact, public;

ALTER TABLE reports
  ADD COLUMN IF NOT EXISTS original_source_url_redacted TEXT NULL;

ALTER TABLE crawled_orgs
  ADD COLUMN IF NOT EXISTS notes TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_reports_discovered_via
  ON reports(discovered_via);  -- supports dashboard queries on wayback rows

INSERT INTO schema_version (version, name)
  VALUES (5, 'wayback_provenance')
  ON CONFLICT (version) DO NOTHING;

COMMIT;
```

**Mirror in `sample_pdfs/migration_005_wayback_provenance.sql`** with same BEFORE/AFTER verification pattern as migration 004 (so the user can re-download for pgAdmin).

**Modify `db_writer.upsert_crawled_org`** to accept `notes` parameter and pass through to the SQL UPSERT.

**Modify `db_writer.upsert_report`** to accept `original_source_url_redacted` and pass through.

**Update `Candidate` dataclass**: add `original_source_url: str | None = None` and `wayback_digest: str | None = None`.

**Modify `_process_download`** to wire `original_source_url_redacted = redact_url(cand.original_source_url)` into `UpsertReportRequest`. If `cand.wayback_digest` is set, append to `RecordFetchRequest.notes` as `wayback_digest:{sha1}` (AC19.1).

**Static-analysis test** (`test_wayback_static_safety.py`, ~50 lines, AC25.1):

```python
def test_original_source_url_never_used_for_outbound_io():
    """AC15.1 + AC25.1: original_source_url must not flow into any
    fetcher / resolver / redirector / classifier-prompt path."""
    repo_root = pathlib.Path(__file__).resolve().parents[5]
    src_files = [
        p for p in repo_root.rglob("lavandula/**/*.py")
        if "tests/" not in str(p) and "__pycache__/" not in str(p)
    ]
    forbidden_callsites = []
    pattern = re.compile(r"original_source_url(_redacted)?")
    for f in src_files:
        text = f.read_text()
        for i, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            # Allow: dataclass field decl, DB write context, log/redact context.
            allowed_contexts = [
                "= None", "field(", "ALTER TABLE",
                "redact_url", "logger.", "_log.", "logging.",
                "original_source_url=", "original_source_url_redacted=",
                "VARCHAR", "TEXT",
            ]
            if any(ctx in line for ctx in allowed_contexts):
                continue
            # Disallow: anything that looks like a fetch / resolve.
            if any(bad in line for bad in [
                "client.get(", "client.head(", "session.get(",
                "session.head(", "urlopen", "requests.", "fetch(",
            ]):
                forbidden_callsites.append(f"{f}:{i}: {line.strip()}")
    
    assert not forbidden_callsites, (
        "original_source_url must not be passed to fetcher/resolver code:\n"
        + "\n".join(forbidden_callsites)
    )
```

**ACs covered**: AC13, AC13.1, AC13.2, AC15.1, AC25.1

**Lines**: ~30 SQL + ~30 src changes + ~50 test

---

## File Summary

| File | Action | Lines |
|------|--------|-------|
| `lavandula/reports/wayback_validation.py` | NEW | ~120 |
| `lavandula/reports/wayback_fallback.py` | NEW | ~250 |
| `lavandula/reports/async_host_throttle.py` | MODIFY | ~30 |
| `lavandula/reports/async_http_client.py` | MODIFY (Retry-After) | ~30 |
| `lavandula/reports/async_discover.py` | MODIFY (wire fallback) | ~50 |
| `lavandula/reports/async_crawler.py` | MODIFY (state machine + active-content reject) | ~70 |
| `lavandula/reports/candidate_filter.py` | MODIFY (Candidate fields) | ~5 |
| `lavandula/reports/db_writer.py` | MODIFY (notes + original_source_url args) | ~15 |
| `lavandula/reports/config.py` | MODIFY (3 new constants) | ~10 |
| `lavandula/migrations/rds/005_wayback_provenance.sql` | NEW | ~30 |
| `sample_pdfs/migration_005_wayback_provenance.sql` | NEW | ~140 |
| **Test files** | | |
| `tests/unit/test_wayback_validation.py` | NEW | ~150 |
| `tests/unit/test_wayback_fallback.py` | NEW | ~300 |
| `tests/unit/test_async_host_throttle.py` | MODIFY | ~40 |
| `tests/unit/test_async_http_client.py` | MODIFY (Retry-After) | ~30 |
| `tests/unit/test_async_discover.py` | MODIFY (gate + classification) | ~80 |
| `tests/unit/test_async_crawler.py` | MODIFY (state machine + active-content) | ~150 |
| `tests/unit/test_wayback_static_safety.py` | NEW | ~50 |
| **Total** | | **~1,550** |

## Dependencies to Install

None. `aiohttp` is already a dependency (Spec 0021).

## Operator Steps Before Deploy

1. Apply migration 005 to RDS (`sample_pdfs/migration_005_wayback_provenance.sql`).
2. Verify the migration log prints DONE and the new columns exist.
3. Deploy code.

## Validation Checklist (Builder)

Before opening the PR:

- [ ] All 38 ACs verified (unit + integration tests).
- [ ] All existing tests still pass (`pytest lavandula/reports/tests/`).
- [ ] New static-analysis test passes (no `original_source_url` in fetcher paths).
- [ ] Smoke test on the 5 known CF-blocked orgs from spec 0022's diagnostic sample. Expected: 4-5 of them recover ≥1 PDF via Wayback (sloan.org, endfund.org definitely).
- [ ] Verify `decisions_log` records `wayback_query` events with all required fields.
- [ ] `lint.sh` clean (if exists) or `ruff check` passes.
- [ ] Migration 005 applied to staging RDS before merge; verify with the AFTER block from the migration script.

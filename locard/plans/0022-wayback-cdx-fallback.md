# Plan 0022 — Wayback Machine CDX Fallback for Cloudflare-blocked Sites

**Spec**: `locard/specs/0022-wayback-cdx-fallback.md`
**Created**: 2026-04-25

---

## Consultation Log

### First Consultation (After Initial Draft)
**Date**: 2026-04-25
**Models Consulted**: Codex ✅, Claude ✅ (Gemini quota exhausted)
**Verdict**: REQUEST_CHANGES (Codex HIGH, Claude HIGH)

| Model | Verdict | Top issues |
|-------|---------|------------|
| Codex | REQUEST_CHANGES (HIGH) | Invalid-domain handling collapsed into ERROR; AC10.1 redirect policy missing; AC19 emission unassigned; two-strikes SQL too loose; Retry-After scope too broad; spec internal contradiction |
| Claude | REQUEST_CHANGES (HIGH) | AC10.1 missing; AC19 emission unwired; AC24.7 idempotency test missing; AC6 timeout not explicit; AC18 counter never incremented; `wayback_all_downloads_failed` flow missing; dead `two_strikes_check=True` flag; AC15.5 body cap not verified |

Combined: 14 distinct issues across both reviewers, all addressed in v2. See "Changes in v2" below.

### Red Team Security Review (MANDATORY)
**Date**: 2026-04-25
**Commands**:
```
consult --model codex  --type red-team-plan plan 0022
consult --model claude --type red-team-plan plan 0022
```

**Verdict**: REQUEST_CHANGES (Codex HIGH, Claude HIGH)

| Model | Verdict | Top issues |
|-------|---------|------------|
| Codex | REQUEST_CHANGES (HIGH) | `DiscoveryResult.wayback_cdx_http_status` field missing — runtime break; `row_count_raw` mis-defined (counts validated, not raw); `wayback_attempts` increments on INVALID_DOMAIN where no query fires; recovered-path `wayback_recoveries++` and AC19 emission unwired |
| Claude | REQUEST_CHANGES (HIGH) | `wayback-cdx` not in `_KIND_TO_CAP` — body cap silently unwired (AC15.5); `stats` not threaded through `discover_via_wayback`/`discover_org` — counter never increments; CDX `digest` interpolated into notes without `sanitize()` — log injection vector; other note interpolations bypass `sanitize()`; `cdx_http_status` lost on EMPTY outcome; static-analysis taint check too shallow (misses indirect assignment + classifier-prompt sinks) |

10 distinct issues across both reviewers (some overlap), all addressed in v3. See "Changes in v3 (post-red-team)" above.

### Changes in v3 (post-red-team)

Codex (HIGH) and Claude (HIGH) red-team plan-review — both REQUEST_CHANGES. 10 distinct issues, all observability/security plumbing gaps that would have shipped silently weakened defenses:

1. **HIGH (Claude): `wayback-cdx` kind not registered in `_KIND_TO_CAP`.** AC15.5 body cap was unwired — Phase 4's `client.get(cdx_url, kind="wayback-cdx", ...)` would have fallen through to a default cap or raised `unknown fetch kind`. **Fix:** Phase 3 explicitly adds `"wayback-cdx": config.MAX_TEXT_BYTES` to the `_KIND_TO_CAP` table in `async_http_client.py`.
2. **HIGH (Claude): `stats` not threaded through `discover_via_wayback` / `discover_org`.** v2 added the parameter to the function signature but didn't update the call site in Phase 5. **Fix:** Phase 5's `discover_org()` signature gains `stats: CrawlStats`; Phase 6's `_process_org_async` passes `stats=stats` through.
3. **HIGH (Claude): CDX `digest` interpolated into `notes` without `sanitize()`.** Log-injection vector via attacker-influenced archive captures. **Fix:** Phase 6 wraps `notes=sanitize(f"wayback_digest:{digest}")` and the same applies to all attacker-touched values entering `notes`.
4. **HIGH (Codex): `DiscoveryResult.wayback_cdx_http_status` field was missing.** Phase 4 produced it; Phase 6 read it; Phase 5 didn't declare it. **Fix:** Phase 5 adds `wayback_cdx_http_status: int | None = None` to `DiscoveryResult` and assigns it from `WaybackResult.cdx_http_status`. AC19 test verifies it's populated.
5. **HIGH (Codex): `row_count_raw` misdefined — collapsed validated count.** Spec wants the raw row count for forensics on schema-drift / malformed-row rates. **Fix:** `WaybackResult` now has `raw_row_count` (total rows from CDX before validation) and `validated_row_count` (survivors of `validate_cdx_row`). Phase 6 logs both: `row_count_raw=raw_row_count`, `row_count_after_dedup=len(candidates)`.
6. **MEDIUM (Codex): `wayback_attempts` increments on INVALID_DOMAIN.** Spec defines as "total CDX queries fired"; INVALID_DOMAIN issues no outbound call. **Fix:** Increment moved to AFTER `_build_cdx_url()` returns non-None and BEFORE `client.get(cdx_url, ...)` actually fires. INVALID_DOMAIN paths exit before the increment. `WaybackResult.cdx_query_fired` documents this.
7. **HIGH (Codex): Recovered-path `wayback_recoveries++` and AC19 emission unwired.** v2 only handled `_record_no_candidates` and `_record_post_download_outcome`; the happy path was implicit. **Fix:** Phase 6 adds `_record_recovered()` called after the org's download barrier when `wayback`-discovered candidates produced ≥1 successful download. Increments `stats.wayback_recoveries` and emits the AC19 event with `outcome="recovered"`.
8. **MEDIUM (Claude): Other note interpolations bypass `sanitize()`.** `wayback_redirect_to_{host}`, `wayback_error`, `wayback_active_content`, etc. pass through the same DB column. **Fix:** all `notes` writes route through `sanitize()` in Phase 3 (`_check_wayback_redirect`'s note) and Phase 6 (every `_enqueue_status` call).
9. **MEDIUM (Claude): `cdx_http_status` lost on EMPTY outcome.** Phase 4's EMPTY-return branch returned `cdx_http_status=None`. AC19 forensics for "empty 200" vs "empty schema-drift" cases would be hampered. **Fix:** All `WaybackResult` returns from `_parse_cdx_response` paths carry the `r.http_status` through, including EMPTY.
10. **MEDIUM (Claude): Static-analysis taint check too shallow.** AST check only inspected fetcher-call args. **Fix:** Phase 7 test extended with two more sink categories: (a) classifier-prompt sinks (`classify(...)`, `prompt(...)`, `complete(...)`, `chat.create(...)`); (b) indirect-assignment chains where `original_source_url` flows through a local variable into a forbidden sink. Inverse test confirms `db_writer.upsert_report` still references the column.

### Changes in v2

Codex (HIGH) and Claude (HIGH) plan-review feedback — both REQUEST_CHANGES. 14 issues addressed:

1. **`WaybackOutcome` adds `INVALID_DOMAIN`.** 4-valued enum: `RECOVERED | EMPTY | ERROR | INVALID_DOMAIN`. Phase 4 returns it; Phase 6 maps it to `notes='wayback_invalid_domain'` distinctly from `wayback_error` (Codex #1).
2. **AC10.1 (Wayback redirect rejection) added to Phase 3.** Cross-host redirects from `web.archive.org` / `archive.org` to non-archive.org hosts are rejected as `blocked_redirect`. New helper `_check_wayback_redirect_chain()` enforced inside `AsyncHTTPClient.get()`'s redirect loop (Codex #2, Claude #1).
3. **AC19 `decisions_log` emission wired into Phase 6.** New helper `_log_wayback_decision()` emits the `wayback_query` event with the bounded enum (`recovered | empty_first | empty_second_promoted | error | all_downloads_failed | invalid_domain`) and required fields (`ein`, `domain`, `cdx_http_status`, `row_count_raw`, `row_count_after_dedup`, `elapsed_ms`, `outcome`, `reason`, `capture_hosts`). Called once per org that triggers Wayback fallback (Codex #3, Claude #2).
4. **Two-strikes SQL CASE fixed and `two_strikes_check` flag removed.** Promotion now requires BOTH `crawled_orgs.status='transient'` AND `crawled_orgs.notes='wayback_no_coverage'` matching the new EXCLUDED row, matching AC15.6 exactly. The dead `two_strikes_check=True` flag from v1 is removed (Codex #4, Claude #2):
   ```sql
   WHEN EXCLUDED.status = 'transient'
        AND EXCLUDED.notes = 'wayback_no_coverage'
        AND crawled_orgs.status = 'transient'
        AND crawled_orgs.notes = 'wayback_no_coverage'
        THEN 'permanent_skip'
   ```
5. **Retry-After scoped to Wayback hosts only.** Phase 3 modification only honors Retry-After for hosts canonicalized to `archive.org` (Codex #5).
6. **Spec reconciliation.** AC24 + AC24.4 in spec 0022 updated to match AC14's two-strikes behavior. Builders can now treat "all ACs verified" literally (Codex #6).
7. **AC6 (15s CDX timeout) explicit.** Phase 4 calls `client.get(cdx_url, kind="wayback-cdx", timeout_override=15.0)` — `AsyncHTTPClient.get()` extended with optional `timeout_override` parameter that supersedes the default 30s for this single call. Test AC26 verifies the override fires (Claude #4).
8. **AC18 `wayback_attempts` increment site.** `discover_via_wayback` increments `stats.wayback_attempts` at function entry (one per CDX query fired). Phase 4 plumbs `stats` parameter; Phase 5 passes it through `discover_org`. Test asserts counter matches outbound CDX request count (Claude #3).
9. **`wayback_all_downloads_failed` flow added.** Phase 6 now has TWO outcome branches: (a) discovery returns no candidates → `_record_no_candidates` (covers RECOVERED-but-empty, EMPTY, ERROR, INVALID_DOMAIN); (b) discovery returns Wayback candidates that all fail download → new `_record_post_download_outcome` checks `discovered_via='wayback'` provenance + zero successful downloads, writes `status='transient'` `notes='wayback_all_downloads_failed'`. Test AC24 covers this fifth state (Claude #6).
10. **AC15.5 (CDX body cap) verified explicitly.** Phase 4 passes `kind="wayback-cdx"` which already maps to `MAX_TEXT_BYTES=5MB` in the existing `_KIND_TO_CAP` table. Phase 4 adds a unit test stubbing a 6MB CDX response and asserting `WaybackOutcome.ERROR` (verifying the existing cap actually fires for this code path) (Claude #5).
11. **AC24.7 idempotency test added.** Test in `test_async_crawler.py` simulates running the Wayback fallback twice for the same org with same CDX response. Asserts: `reports` row count unchanged (no duplicates via `content_sha256` PK), `wayback_recoveries` increments each run (Claude #4).
12. **`AsyncHostPinCache` host-key normalization documented.** Phase 2 explicitly notes that `web.archive.org` and `archive.org` are NOT canonicalized in `AsyncHostPinCache` — pin caching keys on the original hostname (so TLS SNI is preserved per AC15-pin behavior). Only `AsyncHostThrottle` canonicalizes both to one bucket. Test verifies both hosts get separate DNS pins but share one throttle semaphore (Claude #9).
13. **Phase 3's "alternative" discussion removed** (planning-in-progress noise). Plan commits to in-client sleep approach (Claude minor).
14. **Static-analysis test made AST-based** in Phase 7 to avoid the fragile string allowlist Claude flagged. Uses `ast.NodeVisitor` to walk function calls for `client.get`, `client.head`, `session.get`, etc., and asserts `original_source_url` never appears as an argument. Plus an inverse test asserting the expected DB-write call sites *do* contain the field (so a future refactor that drops the field is caught) (Claude #10).

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

### Phase 3: HTTP Client Modifications — Retry-After + Wayback Redirect Policy + Timeout Override + `wayback-cdx` Kind Registration

**Modify**: `lavandula/reports/async_http_client.py` (~85 lines added)

**Four additions, all in `AsyncHTTPClient`:**

#### 3z. Register `wayback-cdx` in `_KIND_TO_CAP` (AC15.5)

Add a single entry to the existing `_KIND_TO_CAP` table so `client.get(cdx_url, kind="wayback-cdx", ...)` correctly applies `MAX_TEXT_BYTES=5MB`. Without this, `client.get` raises `ValueError(f"unknown fetch kind: 'wayback-cdx'")` — Claude's red-team caught this gap.

```python
_KIND_TO_CAP = {
    "robots":           config.MAX_TEXT_BYTES,
    "sitemap":          config.MAX_TEXT_BYTES,
    "homepage":         config.MAX_TEXT_BYTES,
    "subpage":          config.MAX_TEXT_BYTES,
    "pdf-head":         config.MAX_TEXT_BYTES,
    "pdf-get":          config.MAX_PDF_BYTES,
    "classify":         config.MAX_TEXT_BYTES,
    "resolver-verify":  config.MAX_TEXT_BYTES,
    "wayback-cdx":      config.MAX_TEXT_BYTES,  # NEW (Spec 0022 AC15.5)
}
```

#### 3a. Retry-After honor for Wayback hosts only (AC17.2)

After receiving any response with status 429 or 503, if the request host canonicalizes to `archive.org` (per `_canonical_host` from Phase 2), check for a `Retry-After` header and sleep before returning. Other hosts get the response returned unchanged — the retry wrapper at the discovery layer handles them via standard backoff.

```python
async def _maybe_honor_wayback_retry_after(self, host: str, resp) -> None:
    if _canonical_host(host) != "archive.org":
        return
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
        _log.info("Wayback Retry-After=%s, sleeping", retry_after)
        await asyncio.sleep(delay)
```

#### 3b. Wayback cross-host redirect rejection (AC10.1)

In the existing redirect loop in `get()`, if the request was for a Wayback host but the redirect target leaves the archive.org canonical bucket, reject with `blocked_redirect`. Implementation: extend the `check_redirect_chain` call site (or add a sibling check) to also enforce Wayback host invariance.

```python
def _check_wayback_redirect(redirect_chain: list[str]) -> ReceiveCheckResult:
    """If the chain originated at a Wayback host, every hop must remain
    in the archive.org canonical bucket. AC10.1.
    """
    if len(redirect_chain) < 2:
        return ReceiveCheckResult(ok=True, reason=None, note=None)
    origin_host = urlsplit(redirect_chain[0]).hostname or ""
    if _canonical_host(origin_host) != "archive.org":
        return ReceiveCheckResult(ok=True, reason=None, note=None)
    target_host = urlsplit(redirect_chain[-1]).hostname or ""
    if _canonical_host(target_host) != "archive.org":
        # AC15.3 / Claude red-team #8: sanitize attacker-influenced host
        # before interpolation into notes to prevent log injection.
        return ReceiveCheckResult(
            ok=False,
            reason="blocked_redirect",
            note=sanitize(f"wayback_redirect_to_{target_host}"),
        )
    return ReceiveCheckResult(ok=True, reason=None, note=None)
```

Called from inside the redirect loop alongside the existing `check_redirect_chain(seed_etld1)` check. The two checks are independent and both must pass.

#### 3c. Per-call timeout override (AC6)

`AsyncHTTPClient.get()` accepts an optional `timeout_override: float | None = None` kwarg. When provided, that single request uses `aiohttp.ClientTimeout(total=timeout_override, ...)` instead of the session default. Phase 4 uses this for the 15s CDX timeout.

```python
async def get(self, url, *, kind="homepage", seed_etld1=None,
              extra_headers=None, timeout_override: float | None = None):
    ...
    request_kwargs = {"allow_redirects": False, "headers": headers}
    if timeout_override is not None:
        request_kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout_override)
    resp = await session.get(current_url, **request_kwargs)
    ...
```

**Tests** (modify `test_async_http_client.py`, ~80 lines added):
- 3a: 429 with `Retry-After: 5` from `web.archive.org` → asyncio.sleep called with ~5s; 429 from a non-Wayback host → no sleep; 429 from Wayback without `Retry-After` → no sleep; 200 from Wayback with `Retry-After` → no sleep; `Retry-After: bogus` → no sleep, no exception; `Retry-After: 1000` → capped to 60s.
- 3b: redirect from `web.archive.org/web/.../{...}` → `web.archive.org/web/.../{...}` accepted; redirect from `web.archive.org/...` → `evil.com/...` rejected with `blocked_redirect` and `note="wayback_redirect_to_evil.com"`; redirect from a non-Wayback host to anywhere is unaffected by the new check (still uses standard `check_redirect_chain`).
- 3c: `client.get(url, timeout_override=15.0)` uses 15s timeout; calls without override use session default; existing tests unaffected.

**ACs covered**: AC6, AC10.1, AC17.2, AC25.7

**Lines**: ~80 src + ~80 tests

---

### Phase 4: Wayback Discovery Module (`wayback_fallback.py`)

**New file**: `lavandula/reports/wayback_fallback.py` (~250 lines)

**What to build:**

```python
from enum import Enum

class WaybackOutcome(str, Enum):
    RECOVERED       = "recovered"
    EMPTY           = "empty"            # CDX returned cleanly with 0 usable rows
    ERROR           = "error"            # network / 5xx / malformed JSON / oversized body
    INVALID_DOMAIN  = "invalid_domain"   # AC15.2: domain failed RFC-1123 validation; no outbound request issued

@dataclass
class WaybackResult:
    outcome: WaybackOutcome
    candidates: list[Candidate]
    capture_hosts: list[str]                  # for decisions_log
    raw_row_count: int                        # AC19: total rows CDX returned (pre-validation),
                                              # for forensics on schema-drift / malformed-row rates
    validated_row_count: int                  # rows surviving validate_cdx_row
    elapsed_ms: int
    cdx_http_status: int | None = None        # AC19
    cdx_query_fired: bool = False             # True iff client.get(cdx_url) was actually called
                                              # (False on INVALID_DOMAIN). Controls AC18 increment site.

async def discover_via_wayback(
    *,
    seed_url: str,
    seed_etld1: str,
    client: AsyncHTTPClient,
    ein: str,
    stats: CrawlStats,                          # AC18: counter increment site
) -> WaybackResult:
    """Query Wayback CDX for PDFs under the domain, validate rows,
    enforce capture-host policy, return candidates pointing at
    web.archive.org raw-bytes URLs.
    
    Per Codex red-team #6: stats.wayback_attempts increments only when
    an outbound CDX query is actually fired — i.e., AFTER domain
    validation succeeds. INVALID_DOMAIN paths do not increment the
    counter (no network request issued). This matches AC18's spec
    definition "total CDX queries fired".
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


def _parse_cdx_response(body: bytes) -> tuple[WaybackOutcome, list[dict], int]:
    """Parse CDX JSON. Returns (outcome, validated_rows, raw_data_row_count).
    
    AC15.3: row-level validation via validate_cdx_row.
    Codex red-team #5: caller distinguishes raw vs validated count for
    AC19 forensics on schema-drift / malformed-row rates.
    
    State machine per spec section 'State machine: outcome classification':
    - JSON parse failure → ERROR
    - Empty list or header-only → EMPTY (raw_count=0)
    - All rows fail validation → EMPTY (raw_count>0 — useful signal for
      schema-drift forensics; ops can see "CDX returned 30 rows but all
      failed validation")
    - Some rows valid → RECOVERED
    """
    try:
        rows = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (WaybackOutcome.ERROR, [], 0)
    if not isinstance(rows, list) or len(rows) < 1:
        return (WaybackOutcome.EMPTY, [], 0)
    raw_data_rows = rows[1:]   # skip header
    raw_count = len(raw_data_rows)
    validated = []
    for row in raw_data_rows:
        v = validate_cdx_row(row)
        if v is not None:
            validated.append(v)
    if not validated:
        return (WaybackOutcome.EMPTY, [], raw_count)
    return (WaybackOutcome.RECOVERED, validated, raw_count)


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
        # Domain failed validation — AC15.2 / AC25.2. No outbound query
        # fired, so do NOT increment stats.wayback_attempts (Codex red-team #6).
        return WaybackResult(
            outcome=WaybackOutcome.INVALID_DOMAIN,
            candidates=[],
            capture_hosts=[],
            raw_row_count=0,
            validated_row_count=0,
            elapsed_ms=0,
            cdx_http_status=None,
            cdx_query_fired=False,
        )

    # AC18: increment counter only AFTER domain validation succeeds and
    # immediately before the outbound query.
    stats.wayback_attempts += 1
    
    # AC6: explicit 15s timeout override.
    # AC15.5: body cap enforced by AsyncHTTPClient via _KIND_TO_CAP[wayback-cdx]
    #         (registered in Phase 3) which maps to MAX_TEXT_BYTES (5 MB).
    #         Oversized responses return status='size_capped' which we route
    #         to ERROR below.
    r = await client.get(cdx_url, kind="wayback-cdx", timeout_override=15.0)
    elapsed = int((asyncio.get_event_loop().time() - t_start) * 1000)

    if r.status != "ok" or not r.body:
        # 'size_capped' (AC15.5), 'network_error', '5xx', etc. all → ERROR.
        # cdx_http_status preserved for AC19 forensics (Claude red-team #9).
        return WaybackResult(
            outcome=WaybackOutcome.ERROR,
            candidates=[],
            capture_hosts=[],
            raw_row_count=0,
            validated_row_count=0,
            elapsed_ms=elapsed,
            cdx_http_status=r.http_status,
            cdx_query_fired=True,
        )
    
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

**Tests** (`test_wayback_fallback.py`, ~330 lines):
- `_build_cdx_url`: malicious domain rejected (returns None → INVALID_DOMAIN at caller), valid domain produces correctly-encoded URL (AC25.2).
- `_parse_cdx_response`: empty list `[]` → EMPTY; header-only `[[header]]` → EMPTY; all-rows-fail-validation → EMPTY; valid rows → RECOVERED with the validated rows; non-JSON body → ERROR; UnicodeDecodeError → ERROR (AC20, AC20.1, AC24.4, AC24.5).
- `_dedupe_and_cap`:
  - dedup by urlkey picks max timestamp (AC20)
  - sort by timestamp DESC
  - cross-eTLD+1 rows dropped (AC25.4)
  - max 3 subdomains with apex preferred (AC25.4)
  - cap at WAYBACK_MAX_PDFS_PER_ORG (AC7)
- `discover_via_wayback`: full flow with stubbed AsyncHTTPClient — RECOVERED, EMPTY, ERROR, INVALID_DOMAIN, capture_hosts populated, elapsed_ms recorded (AC23, AC24).
- **AC15.5 body-cap verification**: stubbed CDX returning 6 MB body → AsyncHTTPClient returns `status='size_capped'` → `discover_via_wayback` returns `WaybackOutcome.ERROR`, `cdx_http_status=200`, `wayback_attempts++` (verifies the existing cap actually fires for `kind=wayback-cdx`).
- **AC18 counter test**: `wayback_attempts` increments by exactly 1 per `discover_via_wayback` call regardless of outcome (including INVALID_DOMAIN where no network request is issued).
- **AC6 timeout test**: `client.get` is called with `timeout_override=15.0` (verify via mocked `client.get`).

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
async def _record_no_candidates(*, ein, domain, db_actor, stats, discovery):
    """AC14 state machine for orgs whose discovery produced no candidates.
    
    Cases handled here:
    - discovery.wayback_outcome is None       → robots-blocked or other
    - discovery.wayback_outcome == ERROR      → wayback_error
    - discovery.wayback_outcome == EMPTY      → first empty (two-strikes rule)
    - discovery.wayback_outcome == INVALID_DOMAIN → wayback_invalid_domain (AC15.2)
    
    The 'all_downloads_failed' case is handled separately in
    _record_post_download_outcome (after the download barrier).
    """
    notes = "robots_or_unknown"
    log_outcome = "skipped"
    if discovery.wayback_outcome == WaybackOutcome.ERROR:
        stats.wayback_errors += 1
        notes = "wayback_error"
        log_outcome = "error"
    elif discovery.wayback_outcome == WaybackOutcome.INVALID_DOMAIN:
        stats.wayback_errors += 1   # counted as an error; distinct from network errors
        notes = "wayback_invalid_domain"
        log_outcome = "invalid_domain"
    elif discovery.wayback_outcome == WaybackOutcome.EMPTY:
        stats.wayback_empty += 1
        notes = "wayback_no_coverage"
        log_outcome = "empty_first"   # SQL CASE promotes to empty_second on the next observation

    stats.orgs_transient_failed += 1
    # Note values above are static enums — sanitize() is defense-in-depth in
    # case the enum is ever generated dynamically (Claude red-team #8).
    await _enqueue_status(db_actor, ein, "transient", sanitize(notes))
    if discovery.wayback_outcome is not None:
        await _log_wayback_decision(
            db_actor=db_actor, ein=ein, domain=domain,
            outcome=log_outcome, reason=discovery.homepage_failure_reason,
            discovery=discovery,
        )


async def _record_post_download_outcome(
    *, ein, domain, db_actor, stats, discovery,
    successful_downloads: int, candidates: list[Candidate],
):
    """Post-download state machine for Wayback-sourced orgs.
    
    Two cases (Codex red-team #4 + #7):
    - successful_downloads >= 1 → 'recovered' path (AC18: wayback_recoveries++,
      AC19: outcome='recovered')
    - successful_downloads == 0 → 'all_downloads_failed' path (AC14 fifth state)
    
    Only runs when the org's candidates came from Wayback (i.e.,
    discovery.wayback_outcome == RECOVERED). Standard direct-crawl
    orgs are completed by the existing happy-path code in Spec 0021.
    """
    is_wayback = (
        discovery.wayback_outcome == WaybackOutcome.RECOVERED
        and any(c.discovered_via == "wayback" for c in candidates)
    )
    if not is_wayback:
        return  # not a Wayback recovery; standard direct-crawl 'ok' path applies

    if successful_downloads >= 1:
        # Recovered path — AC14 'ok', AC18 wayback_recoveries++, AC19 'recovered'.
        stats.wayback_recoveries += 1
        await _log_wayback_decision(
            db_actor=db_actor, ein=ein, domain=domain,
            outcome="recovered",
            reason=discovery.homepage_failure_reason,
            discovery=discovery,
        )
        # The 'ok' status row itself is written by the existing happy-path
        # code in _process_org_async (UpsertCrawledOrgRequest with status='ok').
        # No additional state write here.
        return

    # All downloads failed — AC14 fifth state.
    stats.orgs_transient_failed += 1
    stats.wayback_errors += 1
    await _enqueue_status(
        db_actor, ein, "transient",
        sanitize("wayback_all_downloads_failed"),
    )
    await _log_wayback_decision(
        db_actor=db_actor, ein=ein, domain=domain,
        outcome="all_downloads_failed",
        reason=discovery.homepage_failure_reason,
        discovery=discovery,
    )


async def _enqueue_status(db_actor, ein, status, notes):
    """Single state-write helper. The SQL CASE in upsert_crawled_org
    handles two-strikes promotion automatically — no flag needed."""
    await db_actor.enqueue(UpsertCrawledOrgRequest(
        ein=ein,
        candidate_count=0,
        fetched_count=0,
        confirmed_report_count=0,
        status=status,
        notes=notes,
    ))


async def _log_wayback_decision(
    *, db_actor, ein, domain, outcome: str, reason: str | None, discovery,
):
    """AC19: emit a wayback_query event to decisions_log with bounded enum
    fields. Implementation uses the existing decisions_log writer.
    """
    from .decisions_log import emit_decision  # existing module
    emit_decision(
        event_type="wayback_query",
        ein=ein,
        domain=domain,
        outcome=outcome,                           # bounded enum
        reason=reason,                             # AC3.1 enum
        cdx_http_status=discovery.wayback_cdx_http_status,
        row_count_raw=discovery.wayback_raw_row_count,
        row_count_after_dedup=len(discovery.candidates) if discovery.wayback_outcome == WaybackOutcome.RECOVERED else 0,
        elapsed_ms=discovery.wayback_elapsed_ms,
        capture_hosts=discovery.wayback_capture_hosts,
    )
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

**Tests** (new tests in `test_async_crawler.py`, ~200 lines added):
- AC14 (full 5-way DB outcome):
  - recovered → `ok`, `wayback_recoveries++`
  - error (wayback CDX failed) → `transient` `wayback_error`, `wayback_errors++`
  - invalid_domain → `transient` `wayback_invalid_domain`, `wayback_errors++`
  - first empty → `transient` `wayback_no_coverage`, `wayback_empty++`
  - second empty (existing row already `status='transient' AND notes='wayback_no_coverage'`) → `permanent_skip`, attempts=2 (AC15.6, AC25.6)
  - all-downloads-failed → `transient` `wayback_all_downloads_failed`, `wayback_errors++`
- AC17.1 / AC25.5: Wayback PDF with embedded JS rejected; archive.put NOT called; `reports` row NOT created; fetch_log row created with notes='wayback_active_content'.
- AC23: integration — homepage 403 cf-mitigated, CDX returns 3 PDFs, all 3 archived, status=ok, wayback_recoveries=1.
- AC24.7 (idempotency): run the Wayback fallback twice for the same org with same CDX response. Assert `reports` row count unchanged on second run (no duplicates via `content_sha256` PK), `wayback_recoveries` increments on each run, `crawled_orgs.attempts` increments.
- **AC19 emission test**: assert `decisions_log` has exactly one `wayback_query` event per Wayback fallback trigger, with all required fields populated (`ein`, `domain`, `cdx_http_status`, `row_count_raw`, `row_count_after_dedup`, `elapsed_ms`, `outcome`, `reason`, `capture_hosts`). Verify `outcome` value is in the bounded enum.

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

**Modify `_process_download`** to wire `original_source_url_redacted = redact_url(cand.original_source_url)` into `UpsertReportRequest`. If `cand.wayback_digest` is set, append to `RecordFetchRequest.notes` as `sanitize(f"wayback_digest:{cand.wayback_digest}")` (AC19.1, Claude red-team #3 — `digest` comes from CDX which is attacker-influenced).

**Wire `_record_post_download_outcome` into the orchestrator.** After the existing per-org download barrier in `_process_org_async`, count successful downloads (where `outcome.status == "ok"` and a `reports` row was written), then call:

```python
# AC18 + AC19 wiring for Wayback orgs (Codex red-team #4, #7).
await _record_post_download_outcome(
    ein=ein,
    domain=urlsplit(website).hostname or seed_etld1,
    db_actor=db_actor,
    stats=stats,
    discovery=discovery,
    successful_downloads=org_fetched[0],
    candidates=candidates,
)
```

For non-Wayback orgs, the function returns early (no state change). For Wayback recoveries, it increments `wayback_recoveries` and emits `outcome='recovered'`. For all-failures, it writes `transient` `wayback_all_downloads_failed`.

**Static-analysis test** (`test_wayback_static_safety.py`, ~80 lines, AC25.1):

AST-based instead of string allowlist (Claude review feedback) so it's robust to formatting changes. Walks the AST of every non-test source file looking for `Call` nodes whose function matches a known fetcher/resolver pattern (`client.get`, `client.head`, `session.get`, `session.head`, `urlopen`, `requests.get`, etc.) and asserts no argument or keyword references `original_source_url` or `original_source_url_redacted`.

Plus an inverse test: assert that `db_writer.upsert_report` is called with `original_source_url_redacted` somewhere in the codebase (so a future refactor that drops the field is caught).

```python
import ast
import pathlib

FORBIDDEN_SINK_NAMES = {
    # Fetcher / resolver sinks (Codex spec red-team)
    "get", "head", "request",   # client/session methods
    "urlopen", "fetch",
    # Classifier / LLM-prompt sinks (Claude plan red-team #6)
    "classify", "prompt", "complete", "create",  # openai-style chat.create, etc.
    "invoke", "ainvoke",                          # langchain-style
    "ask", "generate",                            # generic
}
FORBIDDEN_PROVENANCE_REFS = {"original_source_url", "original_source_url_redacted"}

def _references_provenance(node: ast.AST) -> bool:
    """True if any sub-node directly names the forbidden field."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in FORBIDDEN_PROVENANCE_REFS:
            return True
        if isinstance(child, ast.Attribute) and child.attr in FORBIDDEN_PROVENANCE_REFS:
            return True
    return False


def _build_taint_set_from_assignments(tree: ast.AST) -> set[str]:
    """One-level indirect-assignment chain: collect local names that
    were assigned the value of original_source_url[_redacted]. A
    forbidden sink that consumes these tainted names is also a violation.
    """
    tainted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _references_provenance(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    tainted.add(target.id)
                elif isinstance(target, ast.Tuple):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            tainted.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None and _references_provenance(node.value):
                tainted.add(node.target.id)
    return tainted


def _references_taint(node: ast.AST, tainted: set[str]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in tainted:
            return True
    return False


def test_original_source_url_never_passed_to_forbidden_sink():
    """AC15.1 + AC25.1: original_source_url must not appear (directly or
    via one-level indirect assignment) as an arg to any fetcher / resolver
    / classifier-prompt call."""
    repo_root = pathlib.Path(__file__).resolve().parents[5]
    src_files = [
        p for p in repo_root.rglob("lavandula/**/*.py")
        if "/tests/" not in str(p) and "__pycache__" not in str(p)
    ]
    violations = []
    for f in src_files:
        tree = ast.parse(f.read_text())
        tainted = _build_taint_set_from_assignments(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = (
                node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None)
            )
            if func_name not in FORBIDDEN_SINK_NAMES:
                continue
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if _references_provenance(arg) or _references_taint(arg, tainted):
                    violations.append(f"{f}:{node.lineno}: {func_name}()")
    assert not violations, (
        "original_source_url flowed into a forbidden sink:\n"
        + "\n".join(violations)
    )


def test_original_source_url_redacted_is_actually_written():
    """Inverse: ensure the field IS written to the DB somewhere, so a
    future refactor that drops the column is caught."""
    repo_root = pathlib.Path(__file__).resolve().parents[5]
    src = (repo_root / "lavandula/reports/db_writer.py").read_text()
    assert "original_source_url_redacted" in src, (
        "original_source_url_redacted must be written by db_writer; "
        "if you removed it intentionally, also remove migration 005's column."
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

## Test Environment Setup

Phase 6's integration tests (AC23, AC24, AC24.7) exercise the full orchestrator with stubbed external dependencies. The recommended fixture pattern (reuses Spec 0021's existing test infrastructure):

```python
# Stubbed AsyncHTTPClient that returns canned responses by URL pattern.
@pytest.fixture
def fake_client():
    return _CannedClient({
        # Direct homepage 403 cf-mitigated
        "https://sloan.org": _FakeResult(
            body=b"<html>cf challenge</html>", status="forbidden",
            http_status=403, headers={"server": "cloudflare"},
        ),
        # CDX query response
        "https://web.archive.org/cdx/search/cdx?url=sloan.org/*&...":
            _FakeResult(body=CDX_RESPONSE_FIXTURE, status="ok", http_status=200),
        # Wayback id_ raw bytes
        "https://web.archive.org/web/20260406121250id_/https://sloan.org/...":
            _FakeResult(body=PDF_FIXTURE, status="ok", http_status=200),
    })

# DBWriterActor mock that captures enqueue calls for assertion.
@pytest.fixture
def fake_db_actor(loop):
    return _CapturingDBWriterActor(loop)

# AsyncHostThrottle and AsyncHostPinCache use real instances since
# they're pure asyncio primitives with no I/O. Throttle override map
# is constructed from the production config.
```

Phase 6's tests pass `fake_client`, `fake_db_actor`, and a bare `AsyncHostThrottle(host_overrides={...})` to `_process_org_async`. The decisions_log writer is also stubbed (capture-only) so AC19 assertions can inspect emitted events.

CDX response fixtures live in `lavandula/reports/tests/fixtures/wayback/`:
- `sloan_cdx_recovered.json` — 3 PDFs from sloan.org's apex
- `cdx_empty.json` — `[[header]]`
- `cdx_subdomain_mix.json` — 5 PDFs across 5 distinct subdomains (for AC25.4 cap test)
- `cdx_malformed.json` — `[[header], ["..", "junk"]]` (for AC20.1 schema-drift test)

## Smoke Test Reference Set

The "5 known CF-blocked orgs" referenced in the validation checklist are pinned by EIN here so the smoke test is reproducible across re-runs:

| EIN | Site | Wayback recovery expected |
|---|---|---|
| 131623877 | sloan.org | yes (50+ PDFs in CDX) |
| 136257658 | rffund.org | likely no (1 broken PDF in CDX) |
| 261441650 | endfund.org | yes (annual reports 2012-2013) |
| 273941186 | cbcny.org | yes (homepage archived; PDFs depth-dependent) |
| 274844851 | ktstrust.org | likely no (no homepage snapshot) |

Smoke test passes if **at least 3 of 5** orgs recover ≥1 PDF via Wayback (matching the 60-80% sample recovery rate).

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

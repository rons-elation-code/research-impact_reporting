# Spec 0031: Serpex Search Adapter with Multi-Engine Support

**Status**: Draft
**Author**: Architect
**Dependencies**: Spec 0018 (Gemma Pipeline Resolver)
**Priority**: High

## Problem

The resolver pipeline (Spec 0018) calls the Brave Search API directly at $5/1K queries. We've resolved ~35K orgs so far (~20-25% of the total), with 100K+ remaining. At current rates, completing URL resolution alone will cost $500+, and future search needs (contractor enrichment, address verification) will add more.

Experiment 0001 validated that Serpex (serpex.dev) returns equivalent or better results at $0.30-$0.80/1K queries — a 6-17x cost reduction. On 200 easy-case queries, Serpex matched Brave at 90% top-3 overlap. On 250 hard cases (ambiguous/unresolved/low-confidence), Serpex diverged significantly (30% overlap) but manual review showed it edged ahead: 5 clear wins vs 2 losses in 15 sampled zero-overlap cases, with Serpex finding more specific/local results.

Additionally, the current pipeline is locked to a single search engine. Different engines have different strengths — Google finds .org sites better, Brave finds local businesses better. A multi-engine mode that queries 2+ engines and merges candidates would improve recall on hard cases while still costing less than single-engine Brave direct.

## Goals

1. **Drop-in Serpex replacement** — Swap Brave direct API calls for Serpex with zero changes to downstream pipeline stages (blocklist filtering, HTTP fetch, LLM disambiguation, DB write)
2. **Configurable engine selection** — CLI flag to choose which engine(s) Serpex queries (brave, google, bing, or auto)
3. **Multi-engine mode** — Query N engines per org, merge and dedupe results, pass a wider candidate set to the LLM disambiguator
4. **Cost transparency** — Log which engine(s) were used per query and total credit consumption for the run
5. **Backward compatibility** — Brave direct mode remains available as a fallback (flag to bypass Serpex entirely)
6. **Phone number enrichment** — Separate enrichment pass that uses the search adapter to find org phone numbers from search snippets and website contact pages

## Non-Goals

- Changing the LLM disambiguation stage (Stage 5) — it receives candidates regardless of source
- Changing the blocklist logic — it operates on URLs, engine-agnostic
- Building a generic search abstraction for use outside the resolver — this is resolver-specific
- Caching search results — out of scope for this spec
- Automatic engine routing (e.g., "use Google for .org queries") — manual selection only for now

## Technical Design

### Architecture

```
                    ┌─────────────────────────────────┐
                    │     pipeline_resolver.py         │
                    │         (unchanged)              │
                    │                                  │
                    │  calls: web_search(query, ...)   │
                    └──────────┬──────────────────────┘
                               │
                    ┌──────────▼──────────────────────┐
                    │      web_search.py (NEW)         │
                    │                                  │
                    │  SearchResult(title, url, snip)  │
                    │  search(query, config) → list    │
                    │  search_and_filter(org, config)  │
                    │                                  │
                    │  Dispatches to backend:          │
                    │  ├─ SerpexBackend (default)      │
                    │  └─ BraveDirectBackend (legacy)  │
                    └──────────┬──────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
    ┌─────────▼──┐   ┌────────▼───┐   ┌────────▼───┐
    │  Engine 1  │   │  Engine 2  │   │  Engine N  │
    │  (brave)   │   │  (google)  │   │  (bing)    │
    │            │   │            │   │            │
    │  Serpex    │   │  Serpex    │   │  Serpex    │
    │  API call  │   │  API call  │   │  API call  │
    └────────────┘   └────────────┘   └────────────┘
              │                │                │
              └────────────────┼────────────────┘
                               │
                        merge + dedupe
                               │
                    ┌──────────▼──────────────────────┐
                    │  Deduplicated SearchResult list  │
                    │  (ordered by: first-seen rank,   │
                    │   multi-engine boost)            │
                    └─────────────────────────────────┘
```

### New Module: `lavandula/nonprofits/web_search.py`

Replaces `brave_search.py` as the search interface for the pipeline. `brave_search.py` is preserved but only used when `--search-backend brave-direct` is specified.

```python
@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    engines: tuple[str, ...]  # engines that returned this URL (e.g. ("brave",) or ("brave", "google"))

@dataclass
class SearchConfig:
    backend: str          # "serpex" | "brave-direct"
    engines: list[str]    # ["brave"] or ["brave", "google"] etc.
    api_key: str          # Serpex API key when backend="serpex"; Brave API key when backend="brave-direct"
    qps: float            # rate limit (queries per second)
    count: int            # results per engine query (default 10)
```

For `brave-direct` backend, `SearchConfig.api_key` is populated from the existing `get_brave_api_key()` in `secrets.py`. The CLI determines which secret-loading function to call based on `--search-backend`:
- `serpex` → `get_serpex_api_key()` (new)
- `brave-direct` → `get_brave_api_key()` (existing, unchanged)

#### Public API: `search()`

One public function handles both single and multi-engine modes. No separate `search_multi()`.

```python
def search(query: str, *, config: SearchConfig, rate_limiter: RateLimiter) -> list[SearchResult]:
    """Query configured engine(s) and return results.
    
    Single engine (len(config.engines) == 1): one Serpex API call.
    Multi engine (len(config.engines) > 1): sequential calls, merge+dedupe.
    """
```

Calls Serpex API: `GET https://api.serpex.dev/api/search?q={query}&engine={engine}&category=web`

Internally, multi-engine dispatch is a private `_merge_results()` helper — not a separate public function.

When `config.engines` has >1 entry:
1. Query each engine sequentially (respecting rate limiter between calls)
2. Merge results into a single list
3. Dedupe by normalized URL (see URL Normalization below)
4. Ordering: results that appear in multiple engines rank first (sorted by best rank across engines), then single-engine results by their original rank

#### Blocklist + filter: `search_and_filter()`

**Ownership model**: `web_search.py` owns the full search-and-filter flow. It imports `is_blocked()` from `brave_search.py` (the single source of truth for domain filtering) and applies it internally. `pipeline_resolver.py` no longer calls `is_blocked()` directly — Stage 2 moves into `search_and_filter()`.

```python
def search_and_filter(
    org_name: str, city: str, state: str,
    *, config: SearchConfig, rate_limiter: RateLimiter,
    max_results: int = 3,
) -> list[SearchResult]:
    """Build query, search (single or multi), filter blocklist, return top N."""
```

This means `pipeline_resolver.py` replaces Stage 1 + Stage 2 with a single `search_and_filter()` call. Stages 3-6 remain untouched.

### Changes to Existing Modules

#### `pipeline_resolver.py` — Stages 1+2 collapse into one call

Replace:
```python
from .brave_search import search, BraveSearchError, BraveRateLimiter, is_blocked
```
With:
```python
from .web_search import search_and_filter, SearchError, RateLimiter, SearchConfig
```

The `producer()` function's Stage 1 + Stage 2 collapse from ~30 lines into:
```python
results = search_and_filter(name, city, state, config=search_config, rate_limiter=rate_limiter, max_results=3)
```

`search_and_filter()` handles query construction, search dispatch (single or multi-engine), blocklist filtering, and returns the final filtered list. Stages 3-6 remain identical.

#### `pipeline_resolve.py` (CLI entry point) — New flags

```
--search-backend   serpex | brave-direct          (default: serpex)
--search-engines   brave,google,bing              (default: brave)
--serpex-api-key   literal key                    (mutually exclusive with --serpex-ssm-key)
--serpex-ssm-key   SSM parameter path             (default: lavandula/serpex/api_key)
```

Existing `--brave-qps` flag is renamed to `--search-qps` (with `--brave-qps` kept as deprecated alias).

#### `secrets.py` — New function

```python
def get_serpex_api_key() -> str:
    """Retrieve Serpex API key from SSM or env var."""
```

Short name: `serpex-api-key` (SSM full path: `/cloud2.lavandulagroup.com/serpex-api-key`, following existing `brave-api-key` convention)
Env override: `LAVANDULA_SECRET_SERPEX_API_KEY`

#### Dashboard `pipeline/forms.py` and `views.py`

The resolver form gains a **search engine preset dropdown**:

| Form Value | Label | CLI flag |
|------------|-------|----------|
| `brave` | Brave (default) | `--search-engines brave` |
| `google` | Google | `--search-engines google` |
| `brave,google` | Brave + Google | `--search-engines brave,google` |
| `auto` | Auto (Serpex routing) | `--search-engines auto` |

Default selection: `brave`. `bing` is not in the dashboard dropdown (CLI-only). The dropdown is a new addition alongside the existing LLM model selector. The view passes the form value directly as the `--search-engines` flag when spawning the pipeline subprocess.

### Serpex API Integration

**Endpoint**: `GET https://api.serpex.dev/api/search`

**Headers**: `X-API-Key: {key}`

**Parameters**:
| Param | Value |
|-------|-------|
| `q` | search query |
| `engine` | `brave`, `google`, `bing`, or `auto` |
| `category` | `web` |

**Response** (relevant fields):
```json
{
  "results": [
    {"title": "...", "url": "...", "snippet": "..."}
  ]
}
```

**Rate limits**: Tier-based concurrent requests (10 starter, 50 standard, 100 scale). We enforce our own QPS limit client-side.

**Error handling**: Same retry logic as current Brave client (429/5xx → exponential backoff with 3 retries). Serpex-specific: 402 Payment Required → log credit exhaustion, fail the query (do not retry).

### URL Normalization for Dedup

Used only for merge deduplication — the original URL is preserved on the `SearchResult` object.

```
normalize(url):
  1. Parse with urllib.parse.urlsplit
  2. Lowercase scheme and hostname
  3. Strip "www." prefix from hostname
  4. Strip trailing "/" from path
  5. Remove default ports (:80 for http, :443 for https)
  6. Strip fragment (#...) entirely
  7. Preserve query string as-is (different query params = different pages)
  8. Preserve path as-is (case-sensitive — /About != /about on many servers)
  9. Drop scheme entirely from the normalized key (http://example.com == https://example.com for dedup)
  10. Return: "{hostname}{path}?{query}" (no scheme, no fragment)

  When duplicates merge across schemes, the https URL wins (preferred for fetch).
  If both are https, the first-seen URL is kept.
```

**Why collapse schemes**: In practice, nonprofit websites universally redirect http→https. Search engines inconsistently return one or the other for the same page. Keeping them separate would create false "multi-engine agreement" signals when both engines found the same page but one returned http and the other https. This is a dedup optimization, not a security decision — Stage 3 fetches the actual URL and follows redirects regardless.

This is intentionally conservative overall: it merges obvious duplicates (www vs non-www, http vs https, trailing slash) without collapsing pages that are genuinely different.

### Engine Validation Rules

**Valid engines**: `brave`, `google`, `bing`, `auto`

- `auto` **cannot** be combined with other engines (it is Serpex's own routing — combining defeats the purpose). CLI error if attempted.
- Duplicate engine names are silently deduplicated: `--search-engines brave,brave` → `["brave"]`
- Unknown engine names fail at CLI parse time with a clear error listing valid values
- `--search-backend brave-direct` ignores `--search-engines` entirely (Brave direct has no engine concept)
- `bing` is supported in CLI but not exposed in dashboard UI (untested engine, CLI-only for experimentation)

### Partial Failure & Degraded Search

When multi-engine mode is active and one engine fails while others succeed:

1. The query proceeds with results from surviving engines (AC18)
2. A WARNING log is emitted with the failed engine and error
3. The run tracks three counters: `search_full` (all engines succeeded), `search_partial` (some engines failed), `search_failed` (all engines failed)
4. End-of-run summary reports all three counters
5. Downstream stages (fetch, LLM, DB write) are **not** told about degraded coverage — the LLM disambiguator works with whatever candidates it receives. This is acceptable because:
   - Single-engine mode is the baseline — partial multi-engine is strictly better than single
   - The resolver already handles sparse candidates (sometimes only 1 result passes blocklist)
   - Adding a degraded marker to DB would require schema changes for marginal value

### Empty Results After Filtering

When `search_and_filter()` returns `[]` (all results blocked or no results at all):
- Returns empty list (no error raised)
- Caller (`pipeline_resolver.py`) handles this the same as today: writes `all_blocked` or `no_search_results` to DB
- Metrics: existing `skipped_all_blocked` and `skipped_no_results` counters apply unchanged

### Multi-Engine Merge Algorithm

```
Input: results_by_engine = {engine: [SearchResult, ...]}

1. Normalize each URL per the normalization rules above
2. Build url_map: normalized_url → {engines: set, best_rank: int, result: SearchResult}
3. For each engine's results (in order of config.engines, then by rank within engine):
   - If URL already in url_map, add engine to set, update best_rank = min(current, rank)
   - Else, add new entry with engine set = {engine}, best_rank = rank
4. Sort by: (-len(engines), best_rank)  # multi-engine hits first, then by rank
5. Tie-breaker for equal engine-count and equal rank: order of first insertion (stable sort)
6. Return sorted results
```

This means a URL that appears in both Brave and Google at rank 2 and 5 respectively will sort before a URL that appears only in Brave at rank 1. The intuition: cross-engine agreement is a stronger signal than single-engine rank. The stable-sort tie-breaker ensures deterministic ordering.

### Cost Model

| Mode | Cost per org | Cost for 100K orgs |
|------|-------------|-------------------|
| Brave direct (current) | $0.005 | $500 |
| Serpex single (brave) | $0.0003-$0.0008 | $30-$80 |
| Serpex dual (brave+google) | $0.0006-$0.0016 | $60-$160 |
| Serpex triple (brave+google+bing) | $0.0009-$0.0024 | $90-$240 |

Even triple-engine Serpex is 2-5x cheaper than single-engine Brave direct.

### Logging & Observability

Each search call logs at DEBUG:
- `search.engine={engine} query={query[:50]} results={count} elapsed={ms}ms`

Multi-engine mode additionally logs at INFO:
- `search.multi engines={engines} unique_urls={n} multi_hit_urls={m}`

Partial engine failure logs at WARNING:
- `search.engine_failed engine={engine} error={msg} remaining_engines={list}`

End-of-run summary includes:
- Total queries by engine
- Total unique results
- Multi-engine overlap rate (for multi-engine runs)
- Per-engine failure count
- `search_full` / `search_partial` / `search_failed` counters
- **Estimated credit usage**: `successful_calls × 1 credit` (counts only successful API calls, not retries or failures). Labeled explicitly as "estimated" — actual billing comes from the Serpex dashboard. Retries that succeed count as 1 call. `auto` engine counts as 1 call per query.

### Phone Number Enrichment Pass

A separate enrichment pipeline that uses `web_search.py` to find org phone numbers. Runs independently of the URL resolver — targets orgs that are already resolved (have `website_url`) but lack a phone number.

#### Schema Change

Migration adds a `phone` column to `nonprofits_seed`:

```sql
ALTER TABLE lava_corpus.nonprofits_seed ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE lava_corpus.nonprofits_seed ADD COLUMN IF NOT EXISTS phone_source TEXT;
```

`phone_source` records provenance: `search_snippet`, `website_extract`, or `manual`.

#### Pipeline Design

```
New CLI: pipeline_enrich_phone.py

1. SELECT resolved orgs where phone IS NULL
2. For each org:
   a. Query: "{org_name} {city} {state} phone number"
   b. Extract phone from search snippets using regex
   c. If found in snippets → write directly (no HTTP fetch needed)
   d. If not found in snippets → fetch org's website_url contact/about page,
      extract phone from page text
   e. Validate: US phone format (10 digits, optional +1), not a fax number
   f. Write to nonprofits_seed.phone + phone_source
```

#### Phone Extraction Strategy

**Priority 1 — Search snippets** (cheapest, no HTTP fetch):
Search engines frequently surface phone numbers in Knowledge Panels and local listing snippets. A regex scan of search result snippets catches these at zero additional cost.

```python
_US_PHONE_RE = re.compile(
    r'(?:\+?1[-.\s]?)?'          # optional +1 or 1
    r'\(?(\d{3})\)?[-.\s]?'      # area code
    r'(\d{3})[-.\s]?'            # exchange
    r'(\d{4})'                   # subscriber
)
```

**Priority 2 — Website contact page** (fallback):
If snippets don't contain a phone, fetch the org's known `website_url` and look for `/contact`, `/about`, or `/about-us` pages. Extract phone from page text using the same regex. This reuses the existing HTTP fetch infrastructure from Stage 3 of the resolver (SSRF protections, timeouts, size limits).

**Validation rules**:
- Must be 10-digit US number (with optional +1 prefix)
- Reject known non-phone patterns: fax numbers (skip if preceded by "fax" within 20 chars)
- Reject toll-free numbers starting with 800/888/877/866/855/844/833 (these are call centers, not local offices — configurable)
- If multiple phones found, prefer the one closest to the org name in the text

#### Dashboard Integration

The org detail page (`/orgs/{ein}/`) already shows org metadata. Add `phone` to the display. The pipeline controls page gains an "Enrich Phones" button that triggers the enrichment pipeline for a selected state or all resolved orgs.

#### Cost

Phone enrichment uses 1 Serpex credit per org (single search query). For 100K resolved orgs: ~$30-80 at Serpex rates. Website fetch fallback adds no search cost (uses already-known URLs).

### Migration Path

1. **Phase 1**: Ship `web_search.py` with Serpex backend. Default to `--search-backend serpex --search-engines brave`. This is a 1:1 replacement — same engine, different proxy. Validate with a 100-org run.
2. **Phase 2**: Enable `--search-engines brave,google` for hard cases (re-resolve the ~20K ambiguous/unresolved orgs). Compare resolution rates.
3. **Phase 3**: Phone number enrichment pass on all resolved orgs.
4. **Phase 4**: Once validated, deprecate `--search-backend brave-direct` flag. `brave_search.py` becomes blocklist-only (no more API client code).

## Acceptance Criteria

### Core Search Adapter
1. `web_search.py` module exists with `SearchResult`, `SearchConfig`, `RateLimiter`, `SearchError` types
2. `search()` is the single public search function — handles both single and multi-engine modes
3. `search_and_filter()` owns query construction + search + blocklist filtering (imports `is_blocked()` from `brave_search.py`)
4. Response parsing extracts title, url, snippet from Serpex JSON response
5. `engines` field on `SearchResult` is a `tuple[str, ...]` recording all engines that returned this URL (single-engine: `("brave",)`, multi-engine merged: `("brave", "google")`)

### Multi-Engine Mode
6. `search()` with multiple engines queries each sequentially, respecting rate limiter
7. Results are deduped by normalized URL (per URL Normalization rules: lowercase host, strip www, strip trailing /, remove fragment, normalize scheme, preserve query string and path case)
8. Multi-engine hits sort before single-engine hits at same rank
9. Within same engine count, results sort by best rank across engines; equal engine-count + equal rank uses insertion order (stable sort)
10. `max_results` parameter limits final output after merge+dedupe

### Engine Validation
11. Valid engines: `brave`, `google`, `bing`, `auto`. Unknown values fail at CLI parse with error listing valid options.
12. `auto` cannot be combined with other engines — CLI error if attempted
13. Duplicate engine names silently deduplicated
14. `--search-backend brave-direct` ignores `--search-engines`

### Error Handling
15. Retries on 429/5xx with exponential backoff (2s, 4s, 8s) — same as current Brave client
16. 402 Payment Required logged as credit exhaustion, no retry
17. Network errors (timeouts, connection failures) retried same as status errors
18. Per-engine failures in multi-engine mode do not abort the entire query — remaining engines still run, failure logged at WARNING with engine name and error
19. `SearchError` raised only when ALL engines fail for a query

### Pipeline Integration
20. `pipeline_resolver.py` producer replaces Stage 1 + Stage 2 with single `search_and_filter()` call
21. Stages 3-6 completely unchanged
22. CLI flags: `--search-backend`, `--search-engines`, `--serpex-api-key`, `--serpex-ssm-key`
23. `--search-backend brave-direct` falls back to existing `brave_search.search()` + inline blocklist filter for backward compatibility
24. `--search-qps` controls rate limit (default 1.0), `--brave-qps` kept as deprecated alias that maps to `--search-qps`

### Configuration & Secrets
25. `secrets.py` has `get_serpex_api_key()` reading from SSM path `lavandula/serpex/api_key`
26. Env var override: `LAVANDULA_SECRET_SERPEX_API_KEY`

### Dashboard Integration
27. Resolver form includes search engine preset dropdown with values: `brave` (default), `google`, `brave_google`, `auto`
28. Dashboard resolver trigger passes selected engine(s) as `--search-engines` flag to pipeline subprocess

### Logging
29. Each search call logs engine, query prefix, result count, and latency (DEBUG level)
30. Multi-engine queries log unique URLs and multi-hit count (INFO level)
31. Partial engine failures logged at WARNING with engine name and error message
32. End-of-run summary includes per-engine query counts, per-engine failure counts, search_full/partial/failed counters, and estimated credit usage (successful calls × 1)

### Phone Number Enrichment
33. `pipeline_enrich_phone.py` CLI exists with `--state`, `--limit`, `--search-engines` flags
34. Migration adds `phone` and `phone_source` columns to `nonprofits_seed`
35. Phone regex extracts 10-digit US numbers from search snippets
36. Fax numbers rejected (preceded by "fax" within 20 characters)
37. Toll-free numbers (800/888/877/866/855/844/833) rejected by default, configurable via `--allow-tollfree`
38. Fallback: if no phone in snippets, fetches org's website_url contact/about page
39. Writes `phone` and `phone_source` (`search_snippet` or `website_extract`) to DB
40. Dashboard org detail page displays phone number
41. Dashboard pipeline controls includes "Enrich Phones" trigger

### Testing
42. Unit tests for Serpex API call construction (mock HTTP)
43. Unit tests for multi-engine merge/dedupe algorithm including tie-breaker determinism
44. Unit tests for URL normalization edge cases (www stripping, trailing slash, fragments, query strings, scheme normalization)
45. Unit tests for error handling (402, 429, 5xx, network error, partial engine failure in multi-engine)
46. Unit tests for engine validation (auto+other rejected, duplicates deduped, unknown rejected)
47. Integration test: `search_and_filter()` with mocked Serpex returning known results, verify blocklist applied
48. Backward compatibility test: `--search-backend brave-direct` uses existing Brave client path
49. Unit tests for phone regex extraction (valid US formats, fax rejection, toll-free rejection, multiple phones)
50. Integration test: phone enrichment pipeline with mocked search returning snippet with phone number

## Traps to Avoid

1. **Don't break the blocklist** — `is_blocked()` must remain the single source of truth for domain filtering. Don't duplicate it in `web_search.py`.
2. **Don't change query construction** — The `"{clean_name} {city} {state}"` pattern is battle-tested. Serpex takes the same query string.
3. **Don't parallelize multi-engine queries** — Sequential with rate limiter is safer. Serpex has tier-based concurrency limits and we don't want to burn credits on rate-limit retries.
4. **402 is not retryable** — Credit exhaustion won't resolve on retry. Log it, fail the query, and let the operator know.
5. **Don't remove brave_search.py** — It still owns the blocklist and serves as the fallback backend. Remove only the API client code in Phase 4 (future work, not this spec).
6. **Phone regex false positives** — EINs are 9 digits, zip codes are 5+4 digits, dates look like phone numbers. The phone regex must require exactly 10 digits in phone-like grouping (3-3-4) with separators. Don't extract bare digit sequences without format markers (parens, dashes, dots).
7. **Don't use the phone enrichment blocklist** — The resolver blocklist blocks directory/aggregator sites. For phone enrichment, those sites (Yelp, MapQuest, ChamberOfCommerce) are actually good phone sources. The phone pipeline should NOT apply `is_blocked()` to search results — it uses its own simpler filter (or none).

## Security Considerations

- **API key storage**: Serpex API key stored in AWS SSM (encrypted at rest), same pattern as Brave key. Env var override for local dev only.
- **Key exposure**: API key never logged — existing `log.warning` patterns don't include key, and new Serpex logging follows the same pattern. `SearchConfig.api_key` field is never included in `__repr__`.
- **Query exposure**: Query strings contain org names + city/state — same exposure as current Brave calls, no change in risk profile. Nonprofit names and locations are public IRS data.
- **Third-party proxy trust**: Serpex is a third-party proxy — search queries are visible to them. Same trust model as Brave (queries are nonprofit names + locations, not sensitive data). No PII is sent.
- **SSRF via returned URLs**: Search results contain third-party URLs that Stage 3 will HTTP-fetch. This is unchanged from the current pipeline — Stage 3's existing SSRF protections (private IP blocking, timeout, size limits) apply regardless of search backend. No additional SSRF surface is introduced.
- **Multi-engine provenance**: When multi-engine mode produces more candidates, the existing blocklist filter and LLM disambiguator are the quality gates. More candidates = more URLs to fetch, but each fetch goes through the same protections. The `engine` field on `SearchResult` provides audit trail for which engine sourced each URL.

## Experiment Evidence

See `experiments/0001_serpex_search_comparison/`:
- **Easy cases** (200 resolved orgs): 90% Brave top-3 overlap, 97% domain-level match
- **Hard cases** (250 ambiguous/unresolved/low-confidence): 30% overlap, but manual review of 15 zero-overlap samples showed Serpex 5 wins / 2 losses — divergence was often Serpex finding better candidates
- **Latency**: 2.26s mean, 5.22s p95 (hard cases) — within acceptable range for batch pipeline
- **Reliability**: 450 queries, 0 errors

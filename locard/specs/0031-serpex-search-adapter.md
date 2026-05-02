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
    engine: str  # which engine returned this result

@dataclass
class SearchConfig:
    backend: str          # "serpex" | "brave-direct"
    engines: list[str]    # ["brave"] or ["brave", "google"] etc.
    api_key: str          # Serpex API key (or Brave key for direct)
    qps: float            # rate limit (queries per second)
    count: int            # results per engine query (default 10)
```

#### Single-engine mode (default)

```python
def search(query: str, *, config: SearchConfig, rate_limiter: RateLimiter) -> list[SearchResult]:
    """Query one engine, return results."""
```

Calls Serpex API: `GET https://api.serpex.dev/api/search?q={query}&engine={engine}&category=web`

#### Multi-engine mode

```python
def search_multi(query: str, *, config: SearchConfig, rate_limiter: RateLimiter) -> list[SearchResult]:
    """Query multiple engines sequentially, merge and dedupe results."""
```

When `config.engines` has >1 entry:
1. Query each engine sequentially (respecting rate limiter between calls)
2. Merge results into a single list
3. Dedupe by normalized URL (same logic as experiment: strip www, lowercase, strip trailing slash)
4. Ordering: results that appear in multiple engines rank first (sorted by best rank across engines), then single-engine results by their original rank

#### Blocklist + filter (same interface as brave_search.py)

```python
def search_and_filter(
    org_name: str, city: str, state: str,
    *, config: SearchConfig, rate_limiter: RateLimiter,
    max_results: int = 3,
) -> list[SearchResult]:
    """Build query, search (single or multi), filter blocklist, return top N."""
```

Uses the existing `is_blocked()` from `brave_search.py` — no changes to blocklist logic.

### Changes to Existing Modules

#### `pipeline_resolver.py` — Minimal changes

Replace:
```python
from .brave_search import search, BraveSearchError, BraveRateLimiter
```
With:
```python
from .web_search import search, SearchError, RateLimiter, SearchConfig
```

The `producer()` function's Stage 1 changes from:
```python
raw_results = search(query, api_key=api_key, count=20, rate_limiter=rate_limiter)
```
To:
```python
raw_results = search(query, config=search_config, rate_limiter=rate_limiter)
```

Stage 2 (blocklist filtering) is unchanged — `is_blocked()` stays in `brave_search.py` and operates on URLs.

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

SSM path: `lavandula/serpex/api_key`
Env override: `LAVANDULA_SECRET_SERPEX_API_KEY`

#### Dashboard `pipeline/forms.py` and `views.py`

The resolver form gains a search engine dropdown (or multi-select) so the operator can choose engines from the dashboard UI. This is a cosmetic addition to the existing resolver controls form.

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

### Multi-Engine Merge Algorithm

```
Input: results_by_engine = {engine: [SearchResult, ...]}

1. Normalize each URL: lowercase, strip www., strip trailing /
2. Build url_map: normalized_url → {engines: set, best_rank: int, result: SearchResult}
3. For each engine's results (in order):
   - If URL already in url_map, add engine to set, update best_rank = min(current, rank)
   - Else, add new entry
4. Sort by: (-len(engines), best_rank)  # multi-engine hits first, then by rank
5. Return sorted results
```

This means a URL that appears in both Brave and Google at rank 2 and 5 respectively will sort before a URL that appears only in Brave at rank 1. The intuition: cross-engine agreement is a stronger signal than single-engine rank.

### Cost Model

| Mode | Cost per org | Cost for 100K orgs |
|------|-------------|-------------------|
| Brave direct (current) | $0.005 | $500 |
| Serpex single (brave) | $0.0003-$0.0008 | $30-$80 |
| Serpex dual (brave+google) | $0.0006-$0.0016 | $60-$160 |
| Serpex triple (brave+google+bing) | $0.0009-$0.0024 | $90-$240 |

Even triple-engine Serpex is 2-5x cheaper than single-engine Brave direct.

### Logging & Observability

Each search call logs:
- `search.engine={engine} query={query[:50]} results={count} elapsed={ms}ms`

Multi-engine mode additionally logs:
- `search.multi engines={engines} unique_urls={n} multi_hit_urls={m}`

End-of-run summary includes:
- Total queries by engine
- Total unique results
- Multi-engine overlap rate (for multi-engine runs)

### Migration Path

1. **Phase 1**: Ship `web_search.py` with Serpex backend. Default to `--search-backend serpex --search-engines brave`. This is a 1:1 replacement — same engine, different proxy. Validate with a 100-org run.
2. **Phase 2**: Enable `--search-engines brave,google` for hard cases (re-resolve the ~20K ambiguous/unresolved orgs). Compare resolution rates.
3. **Phase 3**: Once validated, deprecate `--search-backend brave-direct` flag. `brave_search.py` becomes blocklist-only (no more API client code).

## Acceptance Criteria

### Core Search Adapter
1. `web_search.py` module exists with `SearchResult`, `SearchConfig`, `RateLimiter`, `SearchError` types
2. `search()` function calls Serpex API with configurable engine parameter
3. `search_and_filter()` applies existing blocklist logic from `brave_search.py`
4. Response parsing extracts title, url, snippet from Serpex JSON response
5. `engine` field on `SearchResult` records which engine produced the result

### Multi-Engine Mode
6. `search()` with multiple engines queries each sequentially, respecting rate limiter
7. Results are deduped by normalized URL (lowercase, no www, no trailing slash)
8. Multi-engine hits sort before single-engine hits at same rank
9. Within same engine count, results sort by best rank across engines
10. `max_results` parameter limits final output after merge+dedupe

### Error Handling
11. Retries on 429/5xx with exponential backoff (2s, 4s, 8s) — same as current Brave client
12. 402 Payment Required logged as credit exhaustion, no retry
13. Network errors (timeouts, connection failures) retried same as status errors
14. Per-engine failures in multi-engine mode do not abort the entire query — remaining engines still run
15. `SearchError` raised only when ALL engines fail for a query

### Pipeline Integration
16. `pipeline_resolver.py` producer Stage 1 uses `web_search.search()` instead of `brave_search.search()`
17. Stage 2 blocklist filtering unchanged (still uses `brave_search.is_blocked()`)
18. Stages 3-6 completely unchanged
19. CLI flags: `--search-backend`, `--search-engines`, `--serpex-api-key`, `--serpex-ssm-key`
20. `--search-backend brave-direct` falls back to existing `brave_search.search()` for backward compatibility

### Configuration & Secrets
21. `secrets.py` has `get_serpex_api_key()` reading from SSM path `lavandula/serpex/api_key`
22. Env var override: `LAVANDULA_SECRET_SERPEX_API_KEY`
23. `--search-qps` controls rate limit (default 1.0), `--brave-qps` kept as deprecated alias

### Dashboard Integration
24. Resolver form includes engine selection (dropdown or multi-select for engine choice)
25. Dashboard resolver trigger passes selected engine(s) to the pipeline

### Logging
26. Each search call logs engine, query prefix, result count, and latency
27. Multi-engine queries log unique URLs and multi-hit count
28. End-of-run summary includes per-engine query counts

### Testing
29. Unit tests for Serpex API call construction (mock HTTP)
30. Unit tests for multi-engine merge/dedupe algorithm
31. Unit tests for error handling (402, 429, 5xx, network error, partial engine failure)
32. Integration test: `search_and_filter()` with mocked Serpex returning known results, verify blocklist applied
33. Backward compatibility test: `--search-backend brave-direct` uses existing Brave client

## Traps to Avoid

1. **Don't break the blocklist** — `is_blocked()` must remain the single source of truth for domain filtering. Don't duplicate it in `web_search.py`.
2. **Don't change query construction** — The `"{clean_name} {city} {state}"` pattern is battle-tested. Serpex takes the same query string.
3. **Don't parallelize multi-engine queries** — Sequential with rate limiter is safer. Serpex has tier-based concurrency limits and we don't want to burn credits on rate-limit retries.
4. **402 is not retryable** — Credit exhaustion won't resolve on retry. Log it, fail the query, and let the operator know.
5. **Don't remove brave_search.py** — It still owns the blocklist and serves as the fallback backend. Remove only the API client code in Phase 3 (future work, not this spec).

## Security Considerations

- Serpex API key stored in AWS SSM (encrypted at rest), same pattern as Brave key
- API key never logged (existing `log.warning` patterns don't include key)
- Query strings may contain org names — same exposure as current Brave calls, no change in risk profile
- Serpex is a third-party proxy — search queries are visible to them. Same trust model as Brave (queries are nonprofit names + locations, not sensitive data)

## Experiment Evidence

See `experiments/0001_serpex_search_comparison/`:
- **Easy cases** (200 resolved orgs): 90% Brave top-3 overlap, 97% domain-level match
- **Hard cases** (250 ambiguous/unresolved/low-confidence): 30% overlap, but manual review of 15 zero-overlap samples showed Serpex 5 wins / 2 losses — divergence was often Serpex finding better candidates
- **Latency**: 2.26s mean, 5.22s p95 (hard cases) — within acceptable range for batch pipeline
- **Reliability**: 450 queries, 0 errors

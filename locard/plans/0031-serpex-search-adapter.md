# Plan 0031: Serpex Search Adapter with Multi-Engine & Phone Enrichment

**Spec**: `locard/specs/0031-serpex-search-adapter.md`
**Status**: Draft

## Overview

7 implementation phases, building bottom-up: core module → pipeline integration → CLI → dashboard → phone enrichment → tests → validation.

## Phase 1: Core Search Module (`web_search.py`)

**New file**: `lavandula/nonprofits/web_search.py`

### Step 1.1: Types and URL normalization

Create `SearchResult`, `SearchConfig`, `SearchError`, `RateLimiter` types.

```python
@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    engines: tuple[str, ...]

@dataclass
class SearchConfig:
    backend: str          # "serpex" | "brave-direct"
    engines: list[str]    # ["brave"] or ["brave", "google"]
    api_key: str
    qps: float
    count: int = 10
```

`RateLimiter` — copy the token-bucket from `brave_search.py:BraveRateLimiter`, rename. Same logic, thread-safe.

`_normalize_url(url)` — private function implementing the spec's 10-step normalization. Returns `"{host}{path}?{query}"` string.

**ACs**: 1, 7

### Step 1.2: Serpex backend — `_serpex_search()`

Private function that makes a single Serpex API call:

```python
def _serpex_search(query: str, engine: str, *, api_key: str, count: int, rate_limiter: RateLimiter) -> list[SearchResult]:
```

- Endpoint: `GET https://api.serpex.dev/api/search`
- Headers: `X-API-Key: {api_key}`
- Params: `q`, `engine`, `category=web`
- Retry logic: same as `brave_search.search()` — retry on 429/5xx with delays [2.0, 4.0, 8.0]
- 402 → log credit exhaustion, raise `SearchError` immediately (no retry)
- Parse response: `data["results"]` → `SearchResult(title, url, snippet, engines=(engine,))`

**ACs**: 2, 4, 15, 16, 17

### Step 1.3: Multi-engine merge — `_merge_results()`

Private function:

```python
def _merge_results(results_by_engine: dict[str, list[SearchResult]]) -> list[SearchResult]:
```

Implements the spec's merge algorithm:
1. Build `url_map: dict[str, MergeEntry]` keyed by `_normalize_url()`
2. `MergeEntry` tracks: `engines: set`, `best_rank: int`, `result: SearchResult`, `insertion_order: int`
3. When merging, https URL wins over http
4. Sort by `(-len(engines), best_rank, insertion_order)` — stable sort
5. Return list of `SearchResult` with merged `engines` tuples

**ACs**: 7, 8, 9

### Step 1.4: Public `search()` function

```python
def search(query: str, *, config: SearchConfig, rate_limiter: RateLimiter) -> list[SearchResult]:
```

- If `config.backend == "brave-direct"`: delegate to `brave_search.search()`, wrap results as `SearchResult`
- If single engine: call `_serpex_search()` directly
- If multiple engines: loop over `config.engines`, call `_serpex_search()` for each (rate limiter acquired per call), collect results per engine. If one engine fails, log WARNING and continue. If ALL fail, raise `SearchError`. Call `_merge_results()` on collected results.
- Log each call at DEBUG level with engine, query prefix, result count, latency

**ACs**: 2, 5, 6, 18, 19, 23, 29

### Step 1.5: `search_and_filter()` with blocklist

```python
def search_and_filter(
    org_name: str, city: str, state: str,
    *, config: SearchConfig, rate_limiter: RateLimiter,
    max_results: int = 3,
) -> list[SearchResult]:
```

- Builds query: `f'"{sanitized_name}" {city} {state}'` (same sanitization as `brave_search.search_and_filter`)
- Calls `search()`
- Imports `is_blocked` from `brave_search` and filters results
- Returns up to `max_results` non-blocked results
- Returns `[]` if all blocked or no results (no error)

**ACs**: 3, 10, 20

### Step 1.6: Engine validation helper

```python
VALID_ENGINES = frozenset({"brave", "google", "bing", "auto"})

def validate_engines(engines: list[str]) -> list[str]:
    """Validate and deduplicate engine list. Raises ValueError on invalid input."""
```

- Reject unknown engines
- Reject `auto` combined with others
- Deduplicate
- Return cleaned list

**ACs**: 11, 12, 13

## Phase 2: Secrets & Configuration

### Step 2.1: Add `get_serpex_api_key()` to `secrets.py`

```python
def get_serpex_api_key() -> str:
    return get_secret("serpex-api-key")
```

This uses the existing `get_secret()` which prefixes with `/cloud2.lavandulagroup.com/`, giving SSM path: `/cloud2.lavandulagroup.com/serpex-api-key`

Env override (auto-derived by `_env_var_name`): `LAVANDULA_SECRET_SERPEX_API_KEY`

Update `__all__` to export it.

**Note**: The spec says SSM path `lavandula/serpex/api_key` — this is the short_name convention used in the spec. The actual SSM path is determined by `secrets.py`'s prefix convention: `{_PARAM_PREFIX}{short_name}`. The short_name we use is `serpex-api-key` (following `brave-api-key` convention).

**ACs**: 25, 26

## Phase 3: Pipeline Integration

### Step 3.1: Update `pipeline_resolver.py` imports

Replace:
```python
from .brave_search import (BraveRateLimiter, BraveSearchError, search, search_and_filter)
```
With:
```python
from .web_search import (RateLimiter, SearchError, SearchConfig, search_and_filter)
from .brave_search import is_blocked  # only needed if brave-direct fallback inline filtering
```

### Step 3.2: Update `producer()` signature

Add `search_config: SearchConfig` parameter. Remove `api_key: str` and `rate_limiter: BraveRateLimiter` — these are now inside `SearchConfig` / `RateLimiter`.

Actually, keep `rate_limiter` as a separate parameter (it's shared across all producer calls and constructed once at startup). The `SearchConfig` holds the static config; the `RateLimiter` is the runtime state.

### Step 3.3: Collapse Stage 1 + Stage 2 in `producer()`

Replace the ~30 lines of Stage 1 (search) + Stage 2 (blocklist filter) with:

```python
try:
    results = search_and_filter(
        name, city, state,
        config=search_config, rate_limiter=rate_limiter, max_results=3,
    )
except SearchError as exc:
    stats.search_errors += 1
    reason_match = re.search(r"(\d{3})", str(exc))
    status_code = reason_match.group(1) if reason_match else "unknown"
    _write_unresolved(engine, ein, f"search_error:{status_code}", method=method)
    continue

if not results:
    # Preserve split between "no results at all" vs "had results but all blocked"
    # search_and_filter returns ([], True) when results existed but were all blocked
    # vs ([], False) when no results came back from search
    if search_had_results:
        stats.skipped_all_blocked += 1
        _write_unresolved(engine, ein, "all_blocked", method=method)
    else:
        stats.skipped_no_results += 1
        _write_unresolved(engine, ein, "no_search_results", method=method)
    continue
```

To preserve the `all_blocked` vs `no_search_results` distinction, `search_and_filter()` returns a `SearchFilterResult` namedtuple:

```python
class SearchFilterResult(NamedTuple):
    results: list[SearchResult]
    had_raw_results: bool  # True if search returned results (even if all blocked)
```

This lets the caller distinguish "search found nothing" from "search found things but blocklist rejected all of them" without breaking the existing DB reason strings.

Rename `stats.brave_errors` → `stats.search_errors` in `ProducerStats`.

### Step 3.4: Update Stage 3 to use `SearchResult`

Current code accesses `r.title`, `r.snippet`, `r.url` from `BraveSearchResult`. The new `SearchResult` has the same fields plus `engines`. Stage 3 accesses:
- `r.url` — unchanged
- `r.title` — unchanged  
- `r.snippet` — unchanged

No changes needed in Stage 3. The `engines` field is simply not used downstream.

### Step 3.5: Search stats tracking

`web_search.py` maintains a module-level `SearchStats` dataclass (thread-safe via atomic increments):

```python
@dataclass
class SearchStats:
    queries_by_engine: dict[str, int]    # {"brave": 150, "google": 148}
    failures_by_engine: dict[str, int]   # {"google": 2}
    search_full: int = 0                 # all engines succeeded
    search_partial: int = 0              # some engines failed
    search_failed: int = 0              # all engines failed
    
    @property
    def estimated_credits(self) -> int:
        return sum(self.queries_by_engine.values())
```

`search()` increments these after each call. The module exposes `get_search_stats() → SearchStats` and `reset_search_stats()`.

The CLI (`pipeline_resolve.py`) calls `get_search_stats()` at end-of-run and prints alongside the existing pipeline summary:

```
--- Search Summary ---
Engine queries: brave=150, google=148
Engine failures: google=2
Search: 148 full, 2 partial, 0 failed
Estimated credits: 298
```

**ACs**: 29, 30, 31, 32

## Phase 4: CLI Entry Point

### Step 4.1: Update `pipeline_resolve.py` CLI flags

Add new arguments to `_build_parser()`:

```python
p.add_argument("--search-backend", choices=["serpex", "brave-direct"], default="serpex")
p.add_argument("--search-engines", default="brave", help="Comma-separated: brave,google,bing,auto")
p.add_argument("--search-qps", type=float, default=None, help="Search queries per second")

serpex_key_group = p.add_mutually_exclusive_group()
serpex_key_group.add_argument("--serpex-api-key", default=None, help="Serpex API key (literal)")
serpex_key_group.add_argument("--serpex-ssm-key", default=None, help="SSM path for Serpex API key")
```

Keep existing `--brave-qps` but map it:
```python
qps = args.search_qps or args.brave_qps
```

**Post-parse validation** (in `main()`, before any work):
```python
if args.search_backend == "brave-direct":
    engines = ["brave"]  # ignored per spec AC14
else:
    engines = validate_engines(args.search_engines.split(","))
    # validate_engines raises ValueError for unknown engines, auto+other, etc.
    # Catch and call parser.error() for clean CLI output
```

This ensures `--search-backend brave-direct --search-engines google` is silently accepted (engines ignored), while `--search-engines potato` fails immediately.

### Step 4.2: Build `SearchConfig` in `main()`

```python
from lavandula.nonprofits.web_search import SearchConfig, RateLimiter, validate_engines

engines = validate_engines(args.search_engines.split(","))

if args.search_backend == "serpex":
    if args.serpex_api_key:
        search_api_key = args.serpex_api_key
    elif args.serpex_ssm_key:
        search_api_key = get_secret(args.serpex_ssm_key)
    else:
        search_api_key = get_serpex_api_key()
else:
    search_api_key = get_brave_api_key()

search_config = SearchConfig(
    backend=args.search_backend,
    engines=engines,
    api_key=search_api_key,
    qps=qps,
)
rate_limiter = RateLimiter(qps)
```

### Step 4.3: Update `main()` to pass `search_config`

Replace `api_key=api_key` with `search_config=search_config` in the `producer()` call. Update the summary output to include search stats.

**ACs**: 14, 22, 23, 24

## Phase 5: Dashboard Integration

### Step 5.1: Add engine dropdown to `ResolverForm`

In `forms.py`, add to `ResolverForm`:

```python
SEARCH_ENGINE_CHOICES = [
    ("brave", "Brave (default)"),
    ("google", "Google"),
    ("brave_google", "Brave + Google"),
    ("auto", "Auto (Serpex routing)"),
]

search_engines = forms.ChoiceField(
    choices=SEARCH_ENGINE_CHOICES,
    initial="brave",
    widget=forms.Select(attrs={"class": _SELECT}),
    label="Search Engine",
)
```

### Step 5.2: Update `create_resolve_job()` config passthrough

In `views.py` where `ResolverView` builds the config dict for `create_resolve_job()`, add the `search_engines` value from the form:

```python
raw = form.cleaned_data.get("search_engines", "brave")
config["search_engines"] = raw.replace("_", ",")  # "brave_google" → "brave,google"
```

### Step 5.3: Update resolver template

Add the search engine dropdown to `resolver.html` between the LLM preset and brave_qps fields.

### Step 5.4: Rename `brave_qps` field label

Change the label from "Brave QPS" to "Search QPS" in the form. Keep the field name `brave_qps` for backward compat with existing saved configs.

**ACs**: 27, 28

## Phase 6: Phone Number Enrichment

### Step 6.1: Schema migration

New file: `lavandula/migrations/rds/migration_012_phone_enrichment.sql`

```sql
ALTER TABLE lava_corpus.nonprofits_seed ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE lava_corpus.nonprofits_seed ADD COLUMN IF NOT EXISTS phone_source TEXT;
```

**AC**: 34

### Step 6.2: Phone extraction module

New file: `lavandula/nonprofits/phone_extract.py`

```python
_US_PHONE_RE = re.compile(
    r'(?:\+?1[-.\s]?)?'
    r'\(?(\d{3})\)?[-.\s]'       # area code — MUST have separator after
    r'(\d{3})[-.\s]'             # exchange — MUST have separator after
    r'(\d{4})'
    r'(?!\d)'                    # not followed by another digit
)

_FAX_CONTEXT_RE = re.compile(r'\bfax\b', re.I)
_TOLLFREE_PREFIXES = {"800", "888", "877", "866", "855", "844", "833"}

def extract_phone(text: str, *, allow_tollfree: bool = False, org_name: str = "") -> str | None:
    """Extract best valid US phone number from text."""
```

Logic:
1. Find all `_US_PHONE_RE` matches in text with their positions
2. For each match, check 20-char window before for "fax" → skip
3. Check area code against toll-free prefixes → skip unless `allow_tollfree`
4. Collect all valid candidates with their text positions
5. If `org_name` provided and multiple candidates: prefer the phone closest to any occurrence of the org name in the text (character distance from match position to nearest org_name occurrence)
6. If no org_name or only one candidate: return the first valid match
7. Return as normalized `(XXX) XXX-XXXX` string, or `None` if no valid phone found

**ACs**: 35, 36, 37

### Step 6.3: Phone enrichment pipeline

New file: `lavandula/nonprofits/tools/pipeline_enrich_phone.py`

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", help="Filter to orgs in this state")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--search-engines", default="brave")
    parser.add_argument("--allow-tollfree", action="store_true")
    parser.add_argument("--serpex-api-key", default=None)
    parser.add_argument("--search-qps", type=float, default=1.0)
```

Pipeline:
1. Query DB: `SELECT ein, name, city, state, website_url FROM nonprofits_seed WHERE resolver_status = 'resolved' AND phone IS NULL` (with optional `--state` filter and `--limit`)
2. Build `SearchConfig` (same pattern as Phase 4)
3. For each org:
   a. Query Serpex: `"{name} {city} {state} phone number"` — note: NO blocklist filtering (Trap #7)
   b. Scan all result snippets with `extract_phone()`
   c. If found → write to DB with `phone_source = 'search_snippet'`
   d. If not found → fetch `{website_url}/contact`, `{website_url}/about`, `{website_url}/about-us` using the existing `_fetch_candidate()` from `pipeline_resolver.py` (which already has SSRF protections: private IP blocking, timeout, size limits)
   e. Scan page text with `extract_phone()`
   f. If found → write to DB with `phone_source = 'website_extract'`
   g. If still not found → skip (don't write anything)
4. Print summary: total processed, found via snippet, found via website, not found

**ACs**: 33, 38, 39

### Step 6.4: Dashboard phone display

Update org detail template (`org_detail.html`) to show `phone` field if present:

```html
{% if org.phone %}
<dt>Phone</dt>
<dd>{{ org.phone }} <span class="text-xs text-gray-400">({{ org.phone_source }})</span></dd>
{% endif %}
```

**AC**: 40

### Step 6.5: Dashboard "Enrich Phones" trigger

Add a phone enrichment section to the pipeline controls. Minimal: a button + state dropdown that triggers `pipeline_enrich_phone.py` as a background job. Follows the same `create_*_job()` pattern as resolver/crawler/classifier.

**AC**: 41

## Phase 7: Tests

### Step 7.1: Unit tests for `web_search.py`

File: `tests/test_web_search.py`

Tests (mock HTTP via `responses` or `unittest.mock.patch`):

1. **Serpex API call construction** — verify URL, headers, params for single engine call (AC 42)
2. **Response parsing** — title, url, snippet extracted correctly (AC 4)
3. **Multi-engine merge**:
   - Two engines return same URL → merged with both engines, sorted first (AC 43)
   - Tie-breaker: equal engine count + equal rank → insertion order (AC 43)
   - https wins over http in merge (AC 43)
4. **URL normalization**:
   - www stripping, trailing slash, fragments, scheme (AC 44)
   - Query string preserved, path case preserved (AC 44)
5. **Error handling**:
   - 402 → SearchError, no retry (AC 45)
   - 429 → retry with backoff (AC 45)
   - 5xx → retry with backoff (AC 45)
   - Network timeout → retry (AC 45)
   - Multi-engine partial failure: one fails, other succeeds → results from survivor returned (AC 45)
   - Multi-engine all fail → SearchError (AC 45)
6. **Engine validation**:
   - `auto` + `brave` → ValueError (AC 46)
   - Duplicates deduped (AC 46)
   - Unknown engine → ValueError (AC 46)
7. **search_and_filter integration** — mocked Serpex returns 5 results, 2 blocked by `is_blocked()` → 3 returned (AC 47)
8. **Brave-direct fallback** — `backend="brave-direct"` delegates to `brave_search.search()` (AC 48)

### Step 7.2: Unit tests for `phone_extract.py`

File: `tests/test_phone_extract.py`

Tests (AC 49):
1. Valid formats: `(555) 123-4567`, `555-123-4567`, `555.123.4567`, `+1 555 123 4567`, `1-555-123-4567`
2. Fax rejection: `Fax: (555) 123-4567` → None
3. Toll-free rejection: `(800) 555-1234` → None (default), `(800) 555-1234` with `allow_tollfree=True` → returns it
4. EIN not matched: `123456789` (9 digits, no separators) → None
5. Zip code not matched: `12345-6789` → None
6. Multiple phones: first valid one returned
7. No phone in text → None

### Step 7.3: Integration test for phone pipeline

File: `tests/test_pipeline_enrich_phone.py` (AC 50)

Mock Serpex to return a snippet containing `"Call us at (555) 123-4567"`. Verify:
- Phone extracted correctly
- Written to DB with `phone_source = 'search_snippet'`
- Fallback to website fetch when snippet has no phone

## Validation

After implementation:

1. **Smoke test**: Run `pipeline_resolve.py --search-backend serpex --search-engines brave --state TX --limit 10` and compare resolver outcomes to a `--search-backend brave-direct` run on the same 10 orgs.
2. **Multi-engine test**: Run `--search-engines brave,google --limit 50` and verify merge/dedupe logs appear.
3. **Phone enrichment test**: Run `pipeline_enrich_phone.py --state TX --limit 20` and manually verify 5 phone numbers.

## Files Changed

| File | Change |
|------|--------|
| `lavandula/nonprofits/web_search.py` | **NEW** — core search module |
| `lavandula/nonprofits/phone_extract.py` | **NEW** — phone regex extraction |
| `lavandula/nonprofits/tools/pipeline_enrich_phone.py` | **NEW** — phone enrichment CLI |
| `lavandula/nonprofits/pipeline_resolver.py` | Modify — swap imports, collapse Stage 1+2 |
| `lavandula/nonprofits/tools/pipeline_resolve.py` | Modify — new CLI flags, build SearchConfig |
| `lavandula/common/secrets.py` | Modify — add `get_serpex_api_key()` |
| `lavandula/dashboard/pipeline/forms.py` | Modify — add engine dropdown to ResolverForm |
| `lavandula/dashboard/pipeline/views.py` | Modify — pass search_engines to job config |
| `lavandula/dashboard/pipeline/templates/pipeline/resolver.html` | Modify — add engine dropdown |
| `lavandula/dashboard/pipeline/templates/pipeline/org_detail.html` | Modify — show phone field |
| `lavandula/migrations/rds/migration_012_phone_enrichment.sql` | **NEW** — phone + phone_source columns |
| `tests/test_web_search.py` | **NEW** — ~25 unit tests |
| `tests/test_phone_extract.py` | **NEW** — ~10 unit tests |
| `tests/test_pipeline_enrich_phone.py` | **NEW** — integration test |

## Estimated Effort

| Phase | Est. |
|-------|------|
| Phase 1: Core module | 2-3 hours |
| Phase 2: Secrets | 15 min |
| Phase 3: Pipeline integration | 1 hour |
| Phase 4: CLI | 30 min |
| Phase 5: Dashboard | 1 hour |
| Phase 6: Phone enrichment | 2-3 hours |
| Phase 7: Tests | 2-3 hours |
| **Total** | **~10 hours** |

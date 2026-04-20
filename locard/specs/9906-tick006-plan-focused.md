# Plan: TICK-006 Brave-based website resolver

## TICK-006: Brave-based website resolver

**Spec reference**: `locard/specs/0001-nonprofit-seed-list-extraction.md` §TICK-006

**New file**: `lavandula/nonprofits/tools/resolve_websites.py`
**Tests**: `lavandula/nonprofits/tests/unit/test_resolve_websites_006.py` (new)
**Dependency**: `requests` — add to `lavandula/nonprofits/requirements.txt`

---

### Step 1 — CLI and entry point

```
argparse.ArgumentParser()
  --db PATH      seeds.db path (default: <package>/data/seeds.db)
  --limit N      Stop after N lookups (default: no limit; 0 = no limit)
  --qps FLOAT    Queries per second cap (default: 1.0; must be > 0)
  --dry-run      Run queries, print chosen URLs, do NOT write to DB
```

Validate:
- `--qps` must be > 0 → `SystemExit(2)` otherwise
- `--limit` must be >= 0 → `SystemExit(2)` otherwise

API key fetched ONCE at startup via `get_brave_api_key()`.
If `SecretUnavailable` is raised → log error, `sys.exit(1)`.
If key fetched successfully → cached in the process; never logged.

---

### Step 2 — Blocklist and URL validation

```python
BLOCKLIST_HOSTS = frozenset({
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "youtu.be", "tiktok.com",
    "guidestar.org", "propublica.org", "charitynavigator.org",
    "idealist.org", "causeiq.com", "dnb.com", "yelp.com",
    "rocketreach.co", "candid.org", "give.org", "benevity.org",
    "mapquest.com", "chamberofcommerce.com", "zoominfo.com",
    "crunchbase.com", "bloomberg.com", "reddit.com",
})
```

`_is_blocklisted(host: str) -> bool`:
- Lowercase the host before comparison
- Exact match OR suffix match (`host.endswith("." + bad)`)
- `host.endswith("wikipedia.org")` catches all locale subdomains
- ALL `.gov` and `.mil` TLDs blocked (AC12): `host.endswith(".gov") or host.endswith(".mil")`

`_validate_url(url: str) -> str | None`:
Returns canonical `scheme://host` (no port, path, query, fragment) or `None` if invalid. Rules (AC11):
- Parse with `urlsplit()`
- Scheme must be `http` or `https`
- `urlsplit().hostname` (lowercase, port-stripped) must contain at least one `.`
- `urlsplit().hostname` must not contain `xn--` anywhere (punycode rejection)
- `urlsplit().hostname` must not contain any non-ASCII characters (reject raw Unicode)
- `urlsplit().username` must be None (no userinfo)
- Ports are stripped — result is always `f"{scheme}://{hostname}"` (hostname from
  `urlsplit().hostname`, which strips port and lowercases automatically)
- Result never includes path, query, or fragment

`_pick_primary(results: list[dict]) -> str | None`:
- Iterate Brave web results
- For each: extract `url`, run `_validate_url`, check `_is_blocklisted`
- Return first that passes both; return `None` if all fail

---

### Step 3 — Brave API client

```python
def _brave_search(query: str, *, key: str) -> dict:
    r = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        params={"q": query, "count": 10, "safesearch": "moderate"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()
```

Query template: `f'"{name}" {city} nonprofit official website'`
(city may be NULL; if so: `f'"{name}" nonprofit official website'`)

Response body cap (AC13): truncate `website_candidates_json` to 8,192 bytes
(`json.dumps(response)[:8192]`) before DB write.

---

### Step 4 — Retry logic (AC6)

One retry with 30s backoff. On both failures: leave `website_url` NULL,
set `notes = f"brave_error:{status_code_or_exception_type}"`, commit, continue.

`_search_with_retry` takes `brave_search_fn` as an injectable parameter (default
`_brave_search`) so unit tests can pass a mock without monkeypatching:

```python
def _search_with_retry(query, *, key, log, brave_search_fn=_brave_search):
    last_note = "brave_error:unknown"
    for attempt in (1, 2):
        try:
            return brave_search_fn(query, key=key), None
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            last_note = f"brave_error:{code}"
            log.warning("brave_error status=%s attempt=%d", code, attempt)
            if attempt == 1:
                time.sleep(30)
        except Exception as exc:
            last_note = f"brave_error:{type(exc).__name__}"
            log.warning("brave_error type=%s attempt=%d", type(exc).__name__, attempt)
            if attempt == 1:
                time.sleep(30)
    return None, last_note  # both attempts failed; note carries specific error
```

Return value is `(response_or_None, error_note_or_None)` — the note is used
verbatim as the `notes` column value on failure.
```

---

### Step 5 — Main resolution loop

```python
def resolve_batch(conn, *, key, limit, min_sleep, dry_run, log):
    rows = conn.execute(
        "SELECT ein, name, city FROM nonprofits_seed WHERE website_url IS NULL"
        + (" LIMIT ?" if limit else ""),
        (limit,) if limit else (),
    ).fetchall()

    for ein, name, city in rows:
        query = f'"{name}" {city} nonprofit official website' if city \
                else f'"{name}" nonprofit official website'

        t0 = time.monotonic()
        response, error_note = _search_with_retry(query, key=key, log=log)
        elapsed = time.monotonic() - t0

        if response is None:
            notes = error_note  # e.g. "brave_error:429"
            chosen = None
        else:
            results = (response.get("web") or {}).get("results") or []
            chosen = _pick_primary(results)
            notes = None if chosen else "no-non-blocklist-result"

        # Truncation may produce invalid JSON — this column is audit-only;
        # no code path reads it programmatically. Document this explicitly.
        candidates_json = json.dumps(response)[:8192] if response else None

        log.info("org ein=%s name=%s url=%s", ein, name[:40], chosen)

        if dry_run:
            print(f"DRY-RUN ein={ein} url={chosen}")  # stdout for operator inspection
        else:
            conn.execute(
                "UPDATE nonprofits_seed SET website_url=?, website_candidates_json=?, notes=?"
                " WHERE ein=?",
                (chosen, candidates_json, notes, ein),
            )
            conn.commit()  # per-row commit (AC5)

        # Rate limiting: sleep remainder of 1/qps window
        to_sleep = min_sleep - elapsed
        if to_sleep > 0:
            time.sleep(to_sleep)
```

`min_sleep = 1.0 / qps` (computed once from `--qps`).

---

### Step 6 — Unit tests (`test_resolve_websites_006.py`)

All mocked via dependency injection (pass `key` and a mock `requests.get`):

1. **test_blocklist_linkedin** — result with `linkedin.com` host is skipped
2. **test_blocklist_subdomain** — `blog.wikipedia.org` is rejected
3. **test_blocklist_gov** — `.gov` domain is rejected (AC12)
4. **test_all_blocklisted_writes_null** — all 10 results blocklisted → `website_url` NULL, notes="no-non-blocklist-result"
5. **test_picks_first_valid** — first non-blocklisted result chosen, stored as `scheme://host`
6. **test_url_validation_scheme** — `ftp://` and `javascript:` URLs rejected
7. **test_url_validation_punycode** — `xn--nxasmq6b.com` rejected
8. **test_url_validation_no_dot** — `localhost` rejected
9. **test_url_validation_userinfo** — `https://user@host.com` rejected
10. **test_url_canonical_form** — deep URL `https://example.org/about/team` stored as `https://example.org`
11. **test_idempotent_skip** — row with existing `website_url` not queried again (AC2)
12. **test_retry_on_500** — first call raises `HTTPError(500)`, second returns valid result
13. **test_retry_exhausted** — both attempts fail → row left NULL, notes set, no crash
14. **test_qps_sleep** — mock `time.sleep` called with approximately `1/qps` seconds
15. **test_dry_run** — `--dry-run` does not write to DB
16. **test_api_key_startup_failure** — `SecretUnavailable` at startup → `sys.exit(1)` before any DB access
17. **test_response_body_cap** — `website_candidates_json` never exceeds 8,192 bytes
18. **test_limit_flag** — `--limit 2` processes at most 2 rows from a 5-row DB
19. **test_cli_invalid_qps** — `--qps 0` and `--qps -1` each raise `SystemExit(2)`
20. **test_cli_invalid_limit** — `--limit -1` raises `SystemExit(2)`

---

### Step 7 — Live smoke test (skip if Brave key unavailable)

Mark `@pytest.mark.live`:

```python
def test_live_smoke_3_orgs(tmp_path):
    """3 well-known orgs: ILRC, Self-Help for the Elderly, New Roads School.
    Asserts: website_url non-empty, host not blocklisted, scheme is https. (AC10)"""
```

Run with `pytest -m live` explicitly.

---

### Step 8 — Add `requests` dependency

```bash
# lavandula/nonprofits/requirements.txt (or venv pip install)
requests>=2.31
```

Verify `requests` is importable in the nonprofits venv before writing code.

---

### Acceptance Criteria mapping

| AC | Step |
|---|---|
| AC1 (one query, blocklist, write back) | Steps 3 + 5 |
| AC2 (idempotent skip) | Step 5 |
| AC3 (blocklist + null fallback) | Step 2 |
| AC4 (QPS rate limit) | Step 5 |
| AC5 (per-row commit) | Step 5 |
| AC6 (retry with 30s backoff) | Step 4 |
| AC7 (API key once at startup) | Step 1 |
| AC8 (case-insensitive + subdomain) | Step 2 |
| AC9 (unit tests, mocked) | Step 6 |
| AC10 (live smoke test) | Step 7 |
| AC11 (URL validation) | Step 2 |
| AC12 (.gov/.mil blocked) | Step 2 |
| AC13 (8 KB response cap) | Step 3 |
| AC14 (structured logging) | Steps 1 + 5 |
| AC15 (exit codes) | Steps 1 + 4 |

---

### Implementation order

1. Step 8 — verify `requests` in venv first
2. Step 2 — blocklist + URL validation (pure functions, testable immediately)
3. Step 3 — Brave API client
4. Step 4 — retry wrapper
5. Step 1 — CLI + key startup
6. Step 5 — main loop
7. Step 6 — unit tests
8. Step 7 — smoke test

---

### Definition of done

- All 20 unit tests pass with `pytest lavandula/nonprofits/tests/unit/test_resolve_websites_006.py`
- `pytest -m live` smoke test passes against real Brave API
- `python -m lavandula.nonprofits.tools.resolve_websites --help` prints all 4 flags
- `--dry-run` on a seeded DB prints chosen URLs without modifying any rows
- Running on the 100-org coastal `seeds.db` fills `website_url` for all resolvable rows

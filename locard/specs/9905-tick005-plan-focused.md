# Plan: TICK-005 Productionize ProPublica seed enumerator

## TICK-005: Productionize ProPublica seed enumerator

**Spec reference**: `locard/specs/0001-nonprofit-seed-list-extraction.md` §TICK-005

**File**: `lavandula/nonprofits/tools/seed_enumerate.py` (modify in place)
**Tests**: `lavandula/nonprofits/tests/unit/test_seed_enumerate_005.py` (new)

---

### Step 1 — Schema migrations (startup, idempotent)

Add `_apply_migrations(conn)` called at the top of `ensure_db()`:

```python
def _apply_migrations(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "last_page_scanned" not in existing:
        conn.execute("ALTER TABLE runs ADD COLUMN last_page_scanned TEXT DEFAULT NULL")
    if "exit_reason" not in existing:
        conn.execute("ALTER TABLE runs ADD COLUMN exit_reason TEXT DEFAULT NULL")
    existing2 = {row[1] for row in conn.execute("PRAGMA table_info(nonprofits_seed)")}
    if "notes" not in existing2:
        conn.execute("ALTER TABLE nonprofits_seed ADD COLUMN notes TEXT DEFAULT NULL")
    conn.commit()
```

Three additive migrations (new nullable columns) — safe on existing DBs.
`exit_reason` values: `'target_met'` | `'exhausted'` | `'rate_limited'` | `'infra_error'`.

---

### Step 2 — Expand CLI flags

Replace the existing 2-flag argparse with 6 flags. Keep `--target` and `--db` unchanged:

```
--states STATES        comma-separated state codes  (default: CA,NY,MA,WA,OR,CT,NJ,MD,RI)
--ntee-majors CODES    comma-separated NTEE major letters  (default: A,B,E,P)
--revenue-min INT      minimum totrevenue  (default: 1_000_000)
--revenue-max INT      maximum totrevenue  (default: 30_000_000)
--target N             stop after N new orgs added  (default: 100)
--db PATH              seeds.db path  (default: <package>/data/seeds.db)
```

Parse `--states` / `--ntee-majors` as `s.upper().split(",")`. Validate:
- Each state code is 2 uppercase letters → `SystemExit(2)` otherwise
- Each NTEE major is a single uppercase letter → `SystemExit(2)` otherwise
- `--revenue-min` < `--revenue-max` → `SystemExit(2)` otherwise

---

### Step 3 — Filter mismatch guard (AC7)

**Filter identity includes all four filter dimensions**: `states`, `ntee_majors`,
`rev_min`, `rev_max`. Revenue bounds are part of the filter set — changing them
against the same DB is a different run that must use a fresh DB.

Check ALL previous runs for this DB (not just completed ones — an interrupted
run with different filters is still a mismatch):

```python
def _check_filter_consistency(conn, states, ntee_majors, rev_min, rev_max) -> None:
    row = conn.execute(
        "SELECT filters_json FROM runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return  # first run, no conflict
    prev = json.loads(row[0])
    if sorted(prev.get("states", [])) != sorted(states):
        sys.exit(2)  # "Filter mismatch: --states changed; use a different --db"
    if sorted(prev.get("ntee_majors", [])) != sorted(ntee_majors):
        sys.exit(2)  # "Filter mismatch: --ntee-majors changed; use a different --db"
    if prev.get("rev_min") != rev_min or prev.get("rev_max") != rev_max:
        sys.exit(2)  # "Filter mismatch: revenue bounds changed; use a different --db"
```

---

### Step 4 — Cursor/checkpoint

Replace the `pages_scanned` int with a cursor dict `{"{state}:{ntee}": page}`.

**Run lifecycle**:
- On startup, look for the most recent run row where `finished_at IS NULL` AND
  whose `filters_json` matches the current filter set. If found: resume (reuse
  `run_id`, restore cursor). If not: start a new run.
- The filter consistency check (Step 3) runs BEFORE this lookup, so any
  mismatch exits before we get here.

**Cursor semantics**:
- Cursor key: `f"{state}:{ntee_major}"`. Value: last successfully committed page.
- Cursor advances AFTER a successful page fetch + DB commit (not before).
  Crash between fetch and commit leaves cursor at previous page; that page is
  refetched on resume (idempotent — EIN PRIMARY KEY dedup keeps DB consistent).
- **Skipped pages** (JSON parse error, oversized response): cursor does NOT
  advance. On resume the page is retried. This is safe because the skip was
  not a successful commit; retrying is better than silently losing a page.
- After each successful commit: `UPDATE runs SET last_page_scanned=? WHERE run_id=?`
  with `json.dumps(cursor)`.

**429 terminal behavior**: when retries exhausted on a 429, before `sys.exit(0)`:
- `UPDATE runs SET finished_at=?, found_count=? WHERE run_id=?` with
  `exit_reason='rate_limited'` stored in a new `exit_reason TEXT` column
  (add to migration in Step 1). This way a resume attempt sees `finished_at IS NOT NULL`
  and starts a fresh run row rather than treating it as resumable.

---

### Step 5 — HTTP layer with retry (AC8)

Replace the bare `http_get_json` with a `_fetch_with_retry(url, *, consecutive_fail_counter)` function.

**Note**: "error table" in AC8 refers to the behavioral table in the spec — it is NOT a database table; all state lives in `runs.last_page_scanned` and `runs.exit_reason`.

| HTTP status | Retry delays | After retries exhausted |
|---|---|---|
| 429 | 1s, 5s, 30s (3 attempts) | set `exit_reason='rate_limited'`, set `finished_at`, `sys.exit(0)` |
| 5xx / network / timeout | 2s, 10s (2 attempts) | skip `(state, ntee)` for this run; log; continue next pair |
| JSON parse error | — (no retry) | skip this page; log byte length (never body); cursor does NOT advance |
| Oversized response (>1 MB) | — (no retry) | skip this page; WARNING log; cursor does NOT advance |
| Any error | increments `consecutive_fail_counter` | if ≥ 5 → `exit_reason='infra_error'`, `sys.exit(1)` |
| 2xx | resets `consecutive_fail_counter` to 0 | process normally |

`consecutive_fail_counter` is a mutable object passed by reference so the caller
accumulates failures across multiple `(state, ntee)` pairs.

---

### Step 6 — Input validation (security)

Before any DB insert:
- `ein`: must match `re.fullmatch(r'\d{9}', ein)` → skip row if not
- `name`, `city`: truncate to 200 chars before write
- `ntee_code`: take first 6 chars max (ProPublica returns at most 6)

---

### Step 7 — Structured logging (replace print statements)

Switch to `logging.getLogger(__name__)`. Remove all `print()` calls:

```
INFO  "page" state=CA ntee=A page=3 added=2
INFO  "org"  ein=123456789 name="Arts Council..." state=CA
WARNING "http_error" status=429 url=<redacted> attempt=1
INFO  "done" total_added=100 db_rows=543 exit_reason=target_met
```

Never log response body content or raw API URLs in production paths.
(ProPublica has no API key but maintain consistent hygiene.)

---

### Step 8 — Unit tests (`test_seed_enumerate_005.py`)

All mocked — no network:

1. **test_cli_defaults** — parse `[]` args; assert all defaults correct
2. **test_cli_states_flag** — `--states TX,OK` parses to `["TX", "OK"]`
3. **test_cli_invalid_state** — `--states TEXAS` raises `SystemExit(2)`
4. **test_revenue_filter** — mock org endpoint returns rev below `--revenue-min`; org not inserted
5. **test_ntee_filter** — search returns NTEE "Z" org; filtered out
6. **test_cursor_advances** — simulate 2-page fetch; cursor JSON in `runs` reflects page 1 after first iteration
7. **test_resume_uses_cursor** — DB has partial run with cursor `{"TX:A": 2}`; new run starts at page 3
8. **test_filter_mismatch_exits** — existing run with `states=["CA"]`; invoke with `--states TX` → `SystemExit(2)`
9. **test_429_retry_then_exit0** — mock 3× 429 responses; function returns without raising; `runs.finished_at` is set
10. **test_5xx_skips_pair** — mock 2× 500; `(state, ntee)` skipped; loop continues to next pair
11. **test_json_parse_error_skips_page** — mock response is `b"not json"`; page skipped, no crash
12. **test_5_consecutive_failures_exit1** — 5 network errors in a row → `SystemExit(1)`
13. **test_large_response_rejected** — response body > 1 MB → page skipped, WARNING logged
14. **test_ein_validation** — malformed EIN from API → row not inserted
15. **test_name_truncated** — name > 200 chars in API response → stored as first 200 chars
16. **test_idempotent_rerun** — insert EIN once; run again; DB still has exactly 1 row
17. **test_schema_migrations_idempotent** — call `_apply_migrations` twice on same conn; no error

---

### Step 9 — Live smoke test (skip if offline)

In same test file, mark `@pytest.mark.live`:

```python
def test_live_smoke_5_orgs():
    """--target 5 --states MA: adds >= 1 MA org. Skips if ProPublica unreachable."""
    # AC9
```

Run with `pytest -m live` explicitly; default test run excludes it.

---

### Acceptance Criteria mapping

| AC | Step |
|---|---|
| AC1 (CLI flags + SystemExit) | Step 2 |
| AC2 (defaults identical behavior) | Step 2 |
| AC3 (multi-state merge) | Step 2 + 5 |
| AC4 (NTEE filter) | Step 6 (client-side) |
| AC5 (revenue via per-org endpoint) | existing behavior, preserved |
| AC6 (EIN dedup) | existing INSERT OR IGNORE, preserved |
| AC7 (checkpoint + filter mismatch) | Steps 3 + 4 |
| AC8 (rate-limit + error table) | Step 5 |
| AC9 (live smoke test) | Step 9 |
| AC10 (unit tests) | Step 8 |
| AC11 (logging spec) | Step 7 |

---

### Implementation order

1. Step 1 (migrations) — smallest, verifiable immediately
2. Step 2 (CLI) — all tests in category 1-3 become green
3. Step 7 (logging) — replace print() before new code adds more
4. Step 5 (HTTP retry) — tests 9-13
5. Step 3 (filter guard) — test 8
6. Step 4 (cursor) — tests 6-7
7. Step 6 (input validation) — tests 14-15
8. Step 8 (remaining unit tests)
9. Step 9 (smoke test)

Each step is independently committable. Target: one commit per numbered step.

---

### Definition of done

- All 17 unit tests pass with `pytest lavandula/nonprofits/tests/unit/test_seed_enumerate_005.py`
- `pytest -m live` smoke test passes against real ProPublica API
- `python -m lavandula.nonprofits.tools.seed_enumerate --help` prints all 6 flags
- Running with no flags on a fresh DB produces ≥1 row with valid 9-digit EIN
- `_apply_migrations` called twice on existing DB raises no error

# Plan: TICK-008 (focused extract — updated 2026-04-20)

## TICK-008: Capture IRS fields from ProPublica per-org endpoint

**Spec reference**: TICK-008 section in `locard/specs/0001-nonprofit-seed-list-extraction.md`
**File to modify**: `lavandula/nonprofits/tools/seed_enumerate.py`
**Tests to add**: `lavandula/nonprofits/tests/unit/test_seed_enumerate_008.py`

### Step 1 — Add `OrgDetail` dataclass and normalization helpers

At module level in `seed_enumerate.py`, after existing imports:

```python
from dataclasses import dataclass

@dataclass
class OrgDetail:
    revenue: int | None
    ntee_code: str | None
    subsection_code: int | None
    activity_codes: str | None
    classification_codes: str | None
    foundation_code: int | None
    ruling_date: str | None
    accounting_period: int | None

def _to_int(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None

def _to_str(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None
```

### Step 2 — Add 6 migrations to `_apply_migrations`

Append idempotent ALTER TABLE statements — one per new column. Use the same
`try/except OperationalError` pattern as existing migrations (SQLite raises
`OperationalError: duplicate column name` when column already exists).

```sql
ALTER TABLE nonprofits_seed ADD COLUMN subsection_code INTEGER DEFAULT NULL;
ALTER TABLE nonprofits_seed ADD COLUMN activity_codes TEXT DEFAULT NULL;
ALTER TABLE nonprofits_seed ADD COLUMN classification_codes TEXT DEFAULT NULL;
ALTER TABLE nonprofits_seed ADD COLUMN foundation_code INTEGER DEFAULT NULL;
ALTER TABLE nonprofits_seed ADD COLUMN ruling_date TEXT DEFAULT NULL;
ALTER TABLE nonprofits_seed ADD COLUMN accounting_period INTEGER DEFAULT NULL;
```

### Step 3 — Update `_fetch_org_revenue` return type

Replace the current `(revenue, ntee_code)` tuple return with `OrgDetail`.
Use `_to_int` for revenue extraction (consistent with other integer fields):

```python
org = d.get("organization") or {}
filings = d.get("filings_with_data") or []
detail = OrgDetail(
    revenue=_to_int(filings[0]["totrevenue"]) if filings else None,
    ntee_code=_to_str(org.get("ntee_code")),
    subsection_code=_to_int(org.get("subsection_code")),
    activity_codes=_to_str(org.get("activity_codes")),
    classification_codes=_to_str(org.get("classification_codes")),
    foundation_code=_to_int(org.get("foundation_code")),
    ruling_date=_to_str(org.get("ruling_date")),
    accounting_period=_to_int(org.get("accounting_period")),
)
return detail
```

On HTTP error / network exception: continue returning `None` (existing behavior).

**Breaking change note**: Any existing code that unpacks `_fetch_org_revenue` as a
tuple (e.g. `rev, ntee = _fetch_org_revenue(...)`) will break. Search for all
callers and update them. In `enumerate_new_orgs`, the revenue-skip check changes
from `if rev is None: continue` to `if detail is None: continue` (note: `None`
return now means API failure; a zero-revenue org returns `OrgDetail(revenue=None, ...)`,
which should NOT be skipped — verify existing filter logic).

### Step 4 — Update `enumerate_new_orgs` INSERT

Extend the `INSERT OR IGNORE` to include all 6 new columns. Column order must
match the schema definition exactly:

```sql
INSERT OR IGNORE INTO nonprofits_seed
  (ein, name, city, state, ntee_code, revenue,
   subsection_code, activity_codes, classification_codes,
   foundation_code, ruling_date, accounting_period)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

Bind values from `detail.*` using positional `?` placeholders — no string
interpolation.

### Step 5 — Write unit tests in `test_seed_enumerate_008.py`

Six focused tests. Do NOT modify `test_seed_enumerate_005.py`.

| Test | AC | What it checks |
|------|----|----------------|
| `test_new_columns_exist` | AC1 | `ensure_db()` on fresh DB → all 6 columns present in `PRAGMA table_info` |
| `test_migrations_idempotent` | AC2 | `_apply_migrations` called twice → no `OperationalError` |
| `test_fetch_org_revenue_returns_orgdetail` | AC3 | Mock ProPublica JSON → `_fetch_org_revenue` returns `OrgDetail` with correct field values (including `accounting_period=6`) |
| `test_accounting_period_stored` | AC3 | Full enumeration path: mock ProPublica → row in DB has `accounting_period=6` |
| `test_none_return_no_crash` | AC4 | `_fetch_org_revenue` patched to return `None` → `enumerate_new_orgs` does not insert row, no exception |
| `test_malformed_fields` | AC6 | Mock returns `subsection_code=""`, `activity_codes=None`, `foundation_code="bad"` → all stored as NULL |

AC5 verified by running the full TICK-005 test suite unchanged (no new test needed).

### Acceptance checklist

- [ ] `pytest lavandula/nonprofits/tests/unit/test_seed_enumerate_008.py` — 6 tests pass
- [ ] `pytest lavandula/nonprofits/tests/unit/test_seed_enumerate_005.py` — 20 tests still pass (no regressions)
- [ ] `python -m lavandula.nonprofits.tools.seed_enumerate --help` — no import errors
- [ ] Fresh DB after `ensure_db()` has all 6 new columns in `PRAGMA table_info`
- [ ] No tuple-unpacking callers of `_fetch_org_revenue` remain in the codebase

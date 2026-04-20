# Spec: TICK-008 (focused extract — updated 2026-04-20)

### TICK-008: Capture IRS fields from ProPublica per-org endpoint (2026-04-20)

**Summary**: The ProPublica per-org endpoint (`/organizations/{ein}.json`)
already returns IRS-sourced fields beyond `ntee_code` and `totrevenue`.
`seed_enumerate.py` currently discards them. This TICK captures six
additional fields and stores them in `nonprofits_seed`, making IRS
subsection, activity codes, foundation type, ruling date, and fiscal
year end (accounting period) available for downstream filtering and
reporting.

**Motivation**

Fiscal year end (`accounting_period`) is needed to align report-catalogue
crawls with publication cycles. IRS subsection and activity codes improve
lead-generation filtering. All data is already fetched — this is purely
a store-what-we-have change.

**In Scope**

- Add 6 new nullable columns to `nonprofits_seed`
- Add idempotent `ALTER TABLE` migrations in `_apply_migrations`
- Update `_fetch_org_revenue` to return the additional fields
- Update `enumerate_new_orgs` INSERT to store them
- Update unit tests

**Out of Scope**

- IRS EO Business Master File integration (future TICK)
- Back-filling existing rows (new runs will populate; old rows stay NULL)
- Any changes to `resolve_websites.py` or the crawler

**New columns**

```sql
ALTER TABLE nonprofits_seed ADD COLUMN subsection_code INTEGER DEFAULT NULL;
  -- 501(c) type: 3 = 501c3, 4 = 501c4, etc.

ALTER TABLE nonprofits_seed ADD COLUMN activity_codes TEXT DEFAULT NULL;
  -- IRS activity codes string, e.g. "041000000"

ALTER TABLE nonprofits_seed ADD COLUMN classification_codes TEXT DEFAULT NULL;
  -- IRS classification, e.g. "1000"

ALTER TABLE nonprofits_seed ADD COLUMN foundation_code INTEGER DEFAULT NULL;
  -- IRS foundation type: 15 = private foundation, 16 = public charity, etc.

ALTER TABLE nonprofits_seed ADD COLUMN ruling_date TEXT DEFAULT NULL;
  -- Date IRS granted exemption, e.g. "2015-04-01"

ALTER TABLE nonprofits_seed ADD COLUMN accounting_period INTEGER DEFAULT NULL;
  -- Fiscal year end month: 6 = June 30, 12 = December 31
```

All six are additive nullable columns — safe on existing DBs.

**Implementation**

`_fetch_org_revenue` currently returns `(revenue, ntee_code)`. Change
return type to a named dataclass:

```python
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
```

**Failure contract**: `_fetch_org_revenue` returns `None` (not `OrgDetail`) only
when the ProPublica HTTP call itself fails (non-2xx or network error). The caller
in `enumerate_new_orgs` already handles `None` by skipping the row. When the API
call succeeds but individual fields are missing/malformed, the function returns an
`OrgDetail` with those fields set to `None`.

**Field normalization rules** (applied inside `_fetch_org_revenue`):

```python
def _to_int(val) -> int | None:
    """Return int or None; treats None, '', non-numeric as None."""
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None

def _to_str(val) -> str | None:
    """Return stripped string or None for blank/None."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None

org = d.get("organization") or {}
filings = d.get("filings_with_data") or []
detail = OrgDetail(
    revenue=_to_int(filings[0]["totrevenue"]) if filings else None,
    ntee_code=_to_str(org.get("ntee_code")),
    subsection_code=_to_int(org.get("subsection_code")),
    activity_codes=_to_str(org.get("activity_codes")),
    classification_codes=_to_str(org.get("classification_codes")),
    foundation_code=_to_int(org.get("foundation_code")),
    ruling_date=_to_str(org.get("ruling_date")),  # stored verbatim, not validated
    accounting_period=_to_int(org.get("accounting_period")),
)
```

No string truncation — columns are unbounded `TEXT`; store values as-is.

**Insert semantics**: `enumerate_new_orgs` uses `INSERT OR IGNORE`. Existing EIN
rows are not updated — the new columns stay NULL for rows inserted before TICK-008.
Back-filling is explicitly out of scope.

**Security**: All DB writes use parameterized SQL (`?` placeholders). Externally
sourced strings are stored as data only — never interpolated into SQL or emitted
to logs unescaped.

**Acceptance Criteria**

AC1 — Six new columns exist in `nonprofits_seed` after `ensure_db()` on a fresh DB.

AC2 — Migrations are idempotent: calling `_apply_migrations` twice raises no error.
(Test: call `_apply_migrations` on a DB that already has all six columns; no
`OperationalError`.)

AC3 — After enumeration, rows have non-NULL `accounting_period` for orgs whose
ProPublica record includes it (unit test: mock returns `accounting_period=6`,
assert stored value is `6`).

AC4 — When `_fetch_org_revenue` returns `None` (API failure), `enumerate_new_orgs`
skips the row entirely without crashing — preserving the existing `if rev is None:
continue` behavior. (The row is NOT inserted.)

AC5 — Existing TICK-005 unit tests all still pass unchanged.

AC6 — Malformed/blank upstream values are handled safely: unit test with mock
returning `subsection_code=""`, `activity_codes=None`, `foundation_code="bad"`
asserts those fields stored as NULL with no crash or exception.

**Implementation Sizing**

~35 LoC prod changes + ~25 LoC new/updated tests. No new dependencies.

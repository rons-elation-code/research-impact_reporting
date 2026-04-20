# Spec: TICK-008

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
return type to a named dataclass or dict:

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

Extract from the ProPublica response:
```python
org = d.get("organization") or {}
detail = OrgDetail(
    revenue=int(filings[0]["totrevenue"]) if filings else None,
    ntee_code=(org.get("ntee_code") or None),
    subsection_code=org.get("subsection_code"),
    activity_codes=org.get("activity_codes"),
    classification_codes=org.get("classification_codes"),
    foundation_code=org.get("foundation_code"),
    ruling_date=org.get("ruling_date"),
    accounting_period=org.get("accounting_period"),
)
```

Truncate string fields to 50 chars before DB insert.

**Acceptance Criteria**

AC1 — Six new columns exist in `nonprofits_seed` after `ensure_db()` on a fresh DB.

AC2 — Migrations are idempotent: calling `_apply_migrations` twice raises no error.

AC3 — After enumeration, rows have non-NULL `accounting_period` for orgs whose
ProPublica record includes it (unit test: mock returns `accounting_period=6`,
assert stored value is `6`).

AC4 — `_fetch_org_revenue` returning None still works (missing fields stay NULL,
no crash).

AC5 — Existing TICK-005 unit tests all still pass unchanged.

**Implementation Sizing**

~30 LoC prod changes + ~20 LoC new/updated tests. No new dependencies.

# Spec 0001 — TICK-009: Skip per-org API call for existing EINs (2026-04-21)

## Why

`seed_enumerate.enumerate_new_orgs()` currently calls the ProPublica
per-org detail endpoint for *every* EIN returned by the search API, even
EINs already present in `nonprofits_seed`. The detail response is then
discarded by `INSERT OR IGNORE` at DB insert time.

Example: if a search run returns 500 EINs and 400 already exist, the
code burns 400 wasted API calls plus `SLEEP_BETWEEN_CALLS=0.35s` wait
each — about 2+ minutes of wasted wall-clock time and rate-limit
budget per re-run.

For multi-thousand-org batch runs planned next, this waste becomes
significant. Fix is a 3-line check before the detail call.

## Design

Before calling `_fetch_org_revenue(ein, ...)`, check if the EIN already
exists in `nonprofits_seed`:

```python
if conn.execute(
    "SELECT 1 FROM nonprofits_seed WHERE ein = ?", (ein,)
).fetchone():
    continue  # already have this org; skip detail fetch
```

Place the check after the existing per-page `seen` set check and NTEE
filter — those are already cheap in-memory. Add the DB check as the
next cheapest guard before the network call.

## Scope

**In scope**: the skip check in `enumerate_new_orgs` only.

**Out of scope**: refresh mode (re-pulling address/revenue for existing
orgs). Noted as a future TICK — deferred to avoid scope creep.

## Acceptance Criteria

**AC1** — `enumerate_new_orgs` performs a `SELECT 1 FROM nonprofits_seed
WHERE ein = ?` check before `_fetch_org_revenue()`.

**AC2** — When the check returns a row, the per-org fetch is skipped
and the outer loop advances to the next EIN.

**AC3** — Skipped orgs do NOT count against `--target N`. `target` still
means "N *new* orgs added" (unchanged semantics).

**AC4** — Unit test: given a DB pre-populated with EIN `999999999`,
calling `enumerate_new_orgs` with a mocked search response containing
`999999999` never invokes `_fetch_org_revenue` for that EIN.

**AC5** — Unit test: re-running `enumerate_new_orgs` twice with the
same mocked search response makes exactly N detail API calls on run 1
and zero detail API calls on run 2 (for the same EINs).

**AC6** — No changes to schema, `run_id` logging, or
`INSERT OR IGNORE` semantics. The existing `INSERT OR IGNORE` guard
remains as a safety net for any race condition (e.g. EIN discovered
between the SELECT and the INSERT by a parallel run).

## Traps to avoid

1. **Don't skip the EIN-seen-this-run in-memory check.** The new DB
   check is *additional*, not a replacement. The in-memory `seen` set
   still matters for same-run dedup when search results return the
   same EIN across pages.

2. **Don't remove `INSERT OR IGNORE`.** It's the last line of defense
   against concurrent runs or schema edge cases.

3. **Don't widen the check to name/city lookup.** EIN is the only
   authoritative unique key; fuzzy matching is not in scope.

4. **Don't change the `--target` counter.** Skipped-because-existing
   orgs never counted before; they still don't count. Only
   successfully-inserted new orgs advance the counter.

## Files Changed

| File | Change |
|------|--------|
| `lavandula/nonprofits/tools/seed_enumerate.py` | Add existence check before `_fetch_org_revenue` |
| `lavandula/nonprofits/tests/unit/test_seed_enumerate_tick009.py` | NEW — AC4, AC5 |

## Expected impact

Assuming a re-run where 80% of search results are already in the DB:

- Before: 500 search results → 500 detail API calls → ~500 × 0.35s = ~3min idle
- After: 500 search results → ~100 detail calls → ~100 × 0.35s = ~35s idle

Roughly **5x faster** for heavily-overlapping re-runs.
For fresh (no-overlap) runs, zero measurable change (one extra SQL
SELECT per EIN — negligible).

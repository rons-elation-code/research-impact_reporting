# Plan: TICK-009 (focused remediation for scalable report acquisition)

## TICK-009: Stabilize crawl extraction, harden website resolution, and gate curated corpus

**Spec reference**:
- `locard/specs/0001-nonprofit-seed-list-extraction.md` §TICK-006 (website resolver)
- `locard/specs/0004-site-crawl-report-catalogue.md` (seed trust, discovery, classification, public corpus)

**Primary files to modify**:
- `lavandula/reports/fetch_pdf.py`
- `lavandula/reports/crawler.py`
- `lavandula/reports/schema.py`
- `lavandula/reports/db_writer.py`
- `lavandula/nonprofits/tools/resolve_websites.py`
- `lavandula/reports/config.py`

**Tests to add or update**:
- `lavandula/reports/tests/unit/test_fetch_pdf_timeout_009.py`
- `lavandula/reports/tests/unit/test_reports_curated_gating_009.py`
- `lavandula/nonprofits/tests/unit/test_resolve_websites_009.py`
- targeted updates to existing 0004 and 0001 resolver/crawler tests where behavior changes

## Why this TICK exists

The live TX run at `/tmp/tx-test/` exposed three blocking issues that make
the current pipeline unfit for scaled corpus building:

1. **Website resolution accepts obviously wrong domains**
   - Examples observed in `seeds.db`: `greatnonprofits.org`, `theorg.com`,
     `govtribe.com`, `wellness.com`, `intellispect.co`, `whereorg.com`.
   - This poisons the crawl seed list before the report crawler starts.

2. **PDF structure validation timeout is destabilizing the crawler**
   - `signal.alarm(2)` in `fetch_pdf.py` leaks into unrelated code paths.
   - Live logs show `TimeoutError("pdf structure check exceeded 2s")`
     firing during `tick_throttle()` / sitemap fetches, aborting org runs.

3. **Corpus gating is too weak for model-training use**
   - `reports_public` currently admits any non-null classification above the
     confidence threshold, including `not_a_report`.
   - `confirmed_report_count` increments on any non-null classification.
   - `INSERT OR IGNORE` on `content_sha256` freezes first-seen attribution,
     even when later evidence is better.

This TICK does not attempt a full architecture rewrite. It makes the
existing pipeline reliable enough to scale by:
- containing the timeout bug,
- replacing blind first-result website selection with scored resolution,
- separating raw intake from curated/training-ready corpus,
- and preserving better provenance when duplicate PDFs are rediscovered.

## Scope

### In scope

- Replace process-wide PDF alarm timeout with contained validation handling.
- Add explicit extraction success/failure logging.
- Block obvious non-official website domains in the resolver.
- Replace first-valid-result selection with deterministic candidate scoring.
- Persist resolver confidence and resolution method in `nonprofits_seed`.
- Strengthen corpus gating so `not_a_report` cannot enter curated/public views.
- Stop counting every classified row as a confirmed report.
- Replace blind `INSERT OR IGNORE` semantics with evidence-aware duplicate handling.
- Add measurable acceptance checks for scaling readiness.

### Out of scope

- Full agent-orchestration implementation for resolver escalation.
- Full report-year extraction overhaul.
- Full provenance scoring model for every PDF.
- Backfill migration of already-stored bad resolver decisions.
- Search-engine-first architecture revival.

## Workstream 1: Fix PDF timeout handling

### Problem

`_validate_pdf_structure()` in `lavandula/reports/fetch_pdf.py` sets
`signal.alarm(2)` in the main process. In a long-running crawler this is
unsafe because alarms are process-global and can fire after control has
left the validation block.

### Required change

Remove the process-global alarm approach entirely.

Use one of these contained approaches:
- preferred: run structure validation in a short-lived subprocess with an
  explicit wall-clock timeout,
- acceptable: use a worker thread or executor with timeout only if the
  parser work is fully isolated and the timeout cannot interrupt unrelated
  code paths.

### Implementation notes

- Preserve the current contract:
  - valid PDF -> `(True, "")`
  - malformed/timeout PDF -> `(False, reason)`
- Keep the pre-sandbox structural check lightweight.
- Do not allow validation timeout to abort the org crawl.

### Acceptance criteria

- No process-wide `signal.alarm()` remains in `fetch_pdf.py`.
- A structure-check timeout yields a rejected PDF outcome, not an org-level exception.
- New regression test proves unrelated crawler code cannot receive a leaked timeout.

## Workstream 2: Add extraction observability

### Problem

The crawler currently swallows most extraction failures:
- `page_count`, `first_page_text`, and metadata fields remain NULL,
- there is no explicit extraction event in `fetch_log`,
- operators cannot distinguish "PDF stored but parse failed" from
  "run still in progress."

### Required change

Add explicit extraction result logging in `lavandula/reports/crawler.py`.

For each successfully archived PDF:
- log extraction success,
- or log extraction failure with sanitized reason.

If schema expansion is undesirable, encode extraction status in `fetch_log.notes`
using `kind='classify'` only after classification starts, and add a new
`kind='extract'` enum if schema amendment is acceptable. Preferred is a new
`extract` kind for operational clarity.

### Acceptance criteria

- Operators can query how many stored PDFs failed extraction.
- A failed first-page parse does not silently disappear.
- Existing crawl flow keeps running after a per-PDF extraction error.

## Workstream 3: Harden website resolution

### Problem

`resolve_websites.py` currently picks the first syntactically valid,
non-blocklisted Brave result. This is too weak for production data quality.

### Required change

Replace `_pick_primary()` with scored candidate resolution:

#### 3.1 Add a denylist

Immediate denylist additions must include common directory/profile/listing domains
observed in the TX run:
- `greatnonprofits.org`
- `theorg.com`
- `govtribe.com`
- `wellness.com`
- `givefreely.com`
- `whereorg.com`
- `influencewatch.org`
- `foundationcenter.org`
- `intellispect.co`
- `gudsy.org`
- `nursa.com`
- `app.milliegiving.com`
- similar profile/listing hosts discovered during test updates

#### 3.2 Score candidates instead of first-hit select

For each Brave result, compute a confidence score from:
- org-name token match in result title,
- org-name token match in result snippet/description,
- hostname quality,
- root-domain preference over arbitrary deep paths,
- city/state token support when available,
- penalty for directory/profile/listing domains,
- penalty for generic donation/profile/gift-plan subdomains,
- penalty for deep-path-only matches on large institutional hosts,
- bonus for same-brand hostname match.

#### 3.3 Persist resolver metadata

Add additive columns to `nonprofits_seed`:
- `resolver_confidence REAL`
- `resolver_status TEXT`
- `resolver_method TEXT`
- `resolver_reason TEXT`

`resolver_status` enum:
- `accepted`
- `ambiguous`
- `rejected`
- `error`

`resolver_method` initial enum:
- `brave-scored`
- `manual`
- `agent-reviewed`

### Resolution policy

- `score >= 0.85`: accept automatically
- `0.55 <= score < 0.85`: mark ambiguous, do not write `website_url`
- `< 0.55`: reject

This plan intentionally leaves ambiguous rows unresolved instead of inventing certainty.

### Acceptance criteria

- The TX false-positive hosts above are rejected or marked ambiguous.
- Resolver tests prove first-result order no longer determines the chosen URL.
- Every accepted `website_url` stores confidence and resolution method.

## Workstream 4: Strengthen curated corpus gating

### Problem

The current public view is not suitable as a training corpus gate.

### Required change

#### 4.1 Tighten the public/curated view

Update `lavandula/reports/schema.py` so curated/public rows require:
- `classification IN ('annual','impact','hybrid')`
- `classification_confidence >= 0.8`
- `attribution_confidence IN ('own_domain','platform_verified')`
- no active-content flags

Do not admit `other` or `not_a_report`.

If desired, create a new `reports_curated` view and keep `reports_public`
as compatibility shim. If only one view is maintained, it must follow the
stricter rules above.

#### 4.2 Fix `confirmed_report_count`

In `lavandula/reports/crawler.py`, increment `confirmed_report_count` only when
classification is one of:
- `annual`
- `impact`
- `hybrid`

### Acceptance criteria

- `not_a_report` can never appear in curated/public view.
- `other` can remain in raw storage but is excluded from training-ready rows.
- `confirmed_report_count` reflects actual report classifications only.

## Workstream 5: Improve duplicate/provenance handling

### Problem

`db_writer.upsert_report()` uses `INSERT OR IGNORE` keyed by `content_sha256`.
The first-seen attribution wins forever, even if a later discovery has:
- better source org,
- better attribution confidence,
- better source URL,
- or actual classification metadata.

### Required change

Replace blind ignore semantics with one of:

#### Option A — preferred

Add `report_sources` table:
- one row per `(content_sha256, source_org_ein, source_url_redacted, discovered_via)`
- keep `reports` as canonical PDF blob/metadata row
- maintain best attribution on `reports` while preserving alternate discoveries

#### Option B — acceptable short-term

Convert insert to `INSERT ... ON CONFLICT(content_sha256) DO UPDATE` and update
selected fields only when the new evidence is strictly better, e.g.:
- accepted attribution beats unverified
- classified beats unclassified
- populated metadata beats NULL

### Acceptance criteria

- Rediscovering the same PDF with better evidence improves stored provenance.
- Duplicate discovery no longer freezes bad attribution permanently.

## Workstream 6: Scaling readiness metrics

Before scaling from 100s to 1,000s, every batch must emit:
- total seeds
- accepted resolver rows
- ambiguous resolver rows
- rejected resolver rows
- crawled orgs
- fetched PDFs
- stored PDFs
- extracted-text PDFs
- classified PDFs
- curated/public rows
- `not_a_report` count
- extraction failure count

These can be emitted in the existing report generator or a new batch-quality report.

### Acceptance criteria

- Operators can answer "what failed?" from the DB and report outputs.
- Batch scaling decisions are based on measured precision proxies, not row counts.

## File-by-file implementation outline

### `lavandula/reports/fetch_pdf.py`
- Remove `signal.alarm()` timeout logic.
- Add contained validation timeout mechanism.
- Return timeout as per-PDF outcome only.

### `lavandula/reports/crawler.py`
- Add extraction result logging.
- Narrow `confirmed_report_count`.
- Preserve crawl continuity on per-PDF parse failures.

### `lavandula/reports/schema.py`
- Add stricter curated/public gating.
- If needed, add new `fetch_log.kind='extract'`.
- If needed, add `report_sources` table or supporting provenance fields.

### `lavandula/reports/db_writer.py`
- Replace `INSERT OR IGNORE` with evidence-aware conflict handling.
- Support alternate source preservation if `report_sources` is added.

### `lavandula/nonprofits/tools/resolve_websites.py`
- Add denylist entries.
- Replace `_pick_primary()` with scored candidate selection.
- Add schema migrations for resolver metadata.
- Store `resolver_*` fields on write.

### `lavandula/reports/config.py`
- Add resolver thresholds and optional denylist constants if not kept local to resolver.

## Test plan

### New targeted tests

1. `test_pdf_structure_timeout_does_not_leak`
   - Simulate a timed-out validation.
   - Assert no unrelated crawler code path receives the timeout exception.

2. `test_extract_failure_logged`
   - Force PDF parse failure after archive write.
   - Assert extraction failure is visible in DB/logging.

3. `test_reports_curated_excludes_not_a_report`
   - Seed raw row with `classification='not_a_report'`, confidence `0.99`.
   - Assert it is excluded from curated/public view.

4. `test_confirmed_report_count_only_counts_real_reports`
   - Ensure `other` and `not_a_report` do not increment the count.

5. `test_resolver_rejects_directory_domains`
   - Brave results include `greatnonprofits.org`, `theorg.com`, etc.
   - Assert none are auto-accepted.

6. `test_resolver_ambiguous_result_does_not_write_website`
   - Two plausible results close in score.
   - Assert `website_url` remains NULL and resolver status is `ambiguous`.

7. `test_duplicate_pdf_better_evidence_updates_or_preserves_source`
   - Insert weak evidence first, then stronger evidence.
   - Assert stored provenance improves or alternate source is captured.

### Regression suites to rerun

- `pytest lavandula/reports/tests -q`
- `pytest lavandula/nonprofits/tests/unit/test_resolve_websites_006.py -q`
- new 009-focused test files above

## Rollout sequence

### Step 1 — Stability patch
- Land timeout fix and extraction logging.
- Re-run a 10-org batch.
- Verify `first_page_text` and `page_count` populate.

### Step 2 — Resolver hardening
- Land denylist + scoring + resolver metadata.
- Re-run a 25-org batch from known-problem seeds.
- Verify suspicious domains are rejected or ambiguous.

### Step 3 — Corpus gating
- Land curated/public view tightening and confirmed-report fix.
- Re-run classification on extracted rows.
- Verify curated/public count reflects only true report classes.

### Step 4 — Duplicate/provenance handling
- Land conflict-handling update.
- Re-run rows with known duplicate discoveries.

### Step 5 — Measured scaling
- Run 100 seeds.
- Audit random sample of accepted websites and curated reports.
- If precision is acceptable, run 500, then 1,000.

## Acceptance checklist

- [ ] No leaked `pdf structure check exceeded 2s` timeout aborts remain in crawler logs
- [ ] Stored PDFs now produce non-zero `first_page_text` on valid fixtures / live sample
- [ ] `fetch_log` or equivalent shows extraction failure counts explicitly
- [ ] Obvious non-official website domains are never auto-accepted
- [ ] Resolver persists confidence + method metadata
- [ ] `not_a_report` is excluded from curated/public rows
- [ ] `confirmed_report_count` counts only `annual|impact|hybrid`
- [ ] Duplicate PDF rediscovery no longer freezes the first bad attribution forever
- [ ] Batch-quality metrics are queryable after a run

## Open questions to resolve during implementation

1. Whether to create a new `reports_curated` view or tighten `reports_public` in place.
2. Whether duplicate-source tracking should be a new `report_sources` table or a lighter upsert rule.
3. Whether ambiguous resolver rows should be handled later by a separate agent-review queue or left unresolved in v1.

## Recommended commit strategy

Use small commits in this order:

1. `[Spec 0004][Phase: fetch] fix: contain pdf structure timeout`
2. `[Spec 0004][Phase: orchestrate] feat: log extraction outcomes`
3. `[Spec 0001][Phase: resolver] fix: block directory domains and score candidates`
4. `[Spec 0004][Phase: schema] fix: tighten curated corpus gating`
5. `[Spec 0004][Phase: storage] feat: improve duplicate provenance handling`


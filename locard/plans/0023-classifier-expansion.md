# Plan 0023: Classifier Expansion — Full Taxonomy Labels

**Spec**: `locard/specs/0023-classifier-expansion.md`
**Date**: 2026-04-26

## Overview

7 phases, ~800 lines of source + ~600 lines of tests. The implementation touches 6 existing files and adds 1 new module (`taxonomy.py`). Migration 007 adds 3 columns + 3 CHECK constraints + 3 indexes + view update.

## File Inventory

| File | Action | Lines | Phase |
|------|--------|-------|-------|
| `lavandula/reports/taxonomy.py` | NEW | ~120 | 1 |
| `lavandula/reports/classify.py` | MODIFY | +80, -5 | 2 |
| `lavandula/reports/db_writer.py` | MODIFY | +25, -5 | 3 |
| `lavandula/reports/async_crawler.py` | MODIFY | +15, -5 | 4 |
| `lavandula/reports/async_db_writer.py` | MODIFY | +5, -0 | 4 |
| `lavandula/reports/crawler.py` | MODIFY | +15, -5 | 4 |
| `lavandula/reports/tools/classify_null.py` | MODIFY | +30, -10 | 5 |
| `lavandula/migrations/rds/007_classifier_expansion.sql` | NEW | ~60 | 6 |
| `lavandula/reports/tests/unit/test_taxonomy.py` | NEW | ~150 | 1 |
| `lavandula/reports/tests/unit/test_classify_v2.py` | NEW | ~200 | 2 |
| `lavandula/reports/tests/unit/test_drift.py` | NEW | ~80 | 2 |
| `lavandula/reports/tests/unit/test_classify_null_v2.py` | NEW | ~60 | 5 |
| `lavandula/reports/tools/validate_taxonomy_check.py` | NEW | ~50 | 6 |

## Phase 1: Taxonomy Loader (`taxonomy.py`)

**ACs**: AC7, AC8, AC9, AC10, AC11, AC35, AC39

Create `lavandula/reports/taxonomy.py`:

```python
class TaxonomyLoadError(RuntimeError):
    """Raised when collateral_taxonomy.yaml fails validation."""

@dataclass(frozen=True)
class MaterialType:
    id: str
    group: str
    description: str

@dataclass(frozen=True)
class EventType:
    id: str

@dataclass(frozen=True)
class Taxonomy:
    version: int
    material_types: dict[str, MaterialType]   # id -> MaterialType
    event_types: dict[str, EventType]         # id -> EventType
    groups: frozenset[str]                    # distinct group values
    _legacy_map: dict[str, str]               # material_type_id -> legacy classification

    def material_type_to_legacy(self, material_type_id: str) -> str: ...
    def derive_group(self, material_type_id: str) -> str: ...
    def is_valid_material_type(self, mt: str) -> bool: ...
    def is_valid_event_type(self, et: str | None) -> bool: ...

_ID_RE = re.compile(r'^[a-z][a-z0-9_]*$')
_ALLOWED_GROUPS = frozenset({
    'reports', 'campaign', 'invitations', 'programs_journals', 'auction',
    'appeals', 'sponsorship', 'major_gifts', 'planned_giving', 'stewardship',
    'periodic', 'membership', 'day_of_event', 'peer_to_peer',
    'program_services', 'sector_specific', 'other',
})
_MAX_DESCRIPTION_LEN = 200

def load_taxonomy(yaml_path: str | Path | None = None) -> Taxonomy:
    """Load and validate collateral_taxonomy.yaml. Raises TaxonomyLoadError."""
    # Default path: lavandula/docs/collateral_taxonomy.yaml
    # Validates:
    #   - required keys: version, material_types, event_types
    #   - ID regex, no duplicates
    #   - group in _ALLOWED_GROUPS
    #   - description max 200 chars, truncated
    #   - no <untrusted_document> tags in descriptions
    #   - warning if >100 material_types or prompt section >5000 chars

def build_taxonomy_prompt_section(taxonomy: Taxonomy) -> str:
    """Deterministic prompt: sorted by (group, id)."""

# Module-level singleton, loaded once
_taxonomy: Taxonomy | None = None

def get_taxonomy() -> Taxonomy:
    """Return cached taxonomy, loading on first call."""

def ensure_loaded() -> None:
    """Eagerly load taxonomy; raises TaxonomyLoadError on failure.
    Called at crawler/tool startup for fail-fast semantics."""
```

**Eager startup loading**: Crawlers (`async_crawler.py`, `crawler.py`) and tools (`classify_null.py`) MUST call `taxonomy.ensure_loaded()` during their initialization — before entering the work loop. This guarantees fail-fast: a missing or malformed YAML crashes the process immediately at startup rather than silently failing on the first classify call minutes later. `get_taxonomy()` still lazy-loads for test convenience, but production entry points use `ensure_loaded()`.

**Legacy mapping** is hardcoded in `taxonomy.py` (not read from YAML — the mapping is a compatibility concern, not a taxonomy concern):

```python
_MATERIAL_TYPE_TO_LEGACY = {
    "annual_report": "annual",
    "impact_report": "impact",
    "year_in_review": "annual",
    "financial_report": "annual",
    "community_benefit_report": "annual",
    "donor_impact_report": "impact",
    "endowed_fund_report": "impact",
    "not_relevant": "not_a_report",
    # All others map to "other"
}
```

**Tests** (`test_taxonomy.py`):
- `test_load_valid_yaml` — loads real `collateral_taxonomy.yaml`, verifies counts
- `test_missing_file_raises` — TaxonomyLoadError with path in message
- `test_duplicate_material_type_id` — rejects
- `test_duplicate_event_type_id` — rejects
- `test_bad_id_format` — rejects IDs with uppercase, spaces, etc.
- `test_unknown_group` — rejects group not in allowed set
- `test_description_too_long` — truncated to 200 chars
- `test_description_with_untrusted_tags` — rejects
- `test_deterministic_prompt_ordering` — shuffled YAML produces same prompt
- `test_prompt_size_warning` — warns on >100 types (caplog)
- `test_legacy_mapping_complete` — every material_type maps to a valid legacy value
- `test_derive_group` — correct for sample types
- `test_is_valid_material_type` — true for real, false for junk
- `test_is_valid_event_type` — true for real, true for None, false for junk

## Phase 2: Classifier V2 (`classify.py`)

**ACs**: AC12, AC13, AC14, AC15, AC16, AC17, AC18, AC19

Extend `classify.py` with v2 functions alongside v1 (v1 remains for reference but is no longer called):

```python
# New ClassificationResult fields
@dataclasses.dataclass
class ClassificationResult:
    classification: str | None          # legacy, derived
    classification_confidence: float | None
    reasoning: str | None
    classifier_model: str
    input_tokens: int
    output_tokens: int
    error: str = ""
    # V2 fields
    material_type: str | None = None
    material_group: str | None = None
    event_type: str | None = None

CLASSIFIER_TOOL_V2 = { ... }  # per spec

def build_messages_v2(first_page_text: str, taxonomy: Taxonomy) -> tuple[str, str]:
    """System prompt includes taxonomy reference."""

def build_anthropic_kwargs_v2(first_page_text: str, *, model: str | None = None,
                               taxonomy: Taxonomy) -> dict[str, Any]:
    """V2 classifier kwargs with taxonomy-aware prompt and tool schema."""

def _validate_tool_input_v2(data: dict, taxonomy: Taxonomy) -> tuple[str, str, str | None, float, str]:
    """Returns (material_type, material_group, event_type, confidence, reasoning).
    Raises ClassifierError if material_type or event_type invalid."""

def classify_first_page_v2(
    first_page_text: str,
    *,
    client,
    taxonomy: Taxonomy,
    model: str | None = None,
    raise_on_error: bool = True,
) -> ClassificationResult:
    """V2 classifier. Populates all fields including legacy mapping."""
```

Key implementation details:
- `_validate_tool_input_v2` checks `taxonomy.is_valid_material_type()` and `taxonomy.is_valid_event_type()`. If either fails → ClassifierError (AC19 runtime guard).
- `material_group` derived via `taxonomy.derive_group()`, never from LLM.
- `classification` derived via `taxonomy.material_type_to_legacy()`.
- `<untrusted_document>` wrapping preserved unchanged.

**Tests** (`test_classify_v2.py`):
- `test_valid_annual_report` — material_type='annual_report', group='reports', classification='annual'
- `test_valid_event_collateral` — material_type='event_invitation', event_type='gala', group='invitations', classification='other'
- `test_null_event_type_accepted` — event_type=None is valid
- `test_invalid_material_type_rejected` — ClassifierError
- `test_invalid_event_type_rejected` — ClassifierError (AC37)
- `test_material_group_not_from_llm` — even if LLM sends group, we derive from taxonomy
- `test_legacy_mapping_all_types` — parametrized over all material_types (AC29)
- `test_confidence_out_of_range` — ClassifierError
- `test_untrusted_document_tags_preserved` — system/user prompt structure
- `test_prompt_includes_taxonomy` — taxonomy section present in system prompt
- `test_event_shaped_null_event` — event_invitation with event_type=None is valid (AC34)

**Drift tests** (`test_drift.py`):
- `test_all_material_types_have_legacy_mapping` — loads real YAML, checks every ID has mapping (AC32)
- `test_all_material_types_in_check_constraint` — loads real YAML, parses migration SQL, compares (AC33)
- `test_all_event_types_in_check_constraint` — same for event_types
- `test_all_groups_in_check_constraint` — same for groups

## Phase 3: DB Writer (`db_writer.py`)

**ACs**: AC22

Extend `upsert_report` signature and SQL:

```python
def upsert_report(
    engine, *,
    # ... existing params ...
    material_type: str | None = None,        # NEW
    material_group: str | None = None,        # NEW
    event_type: str | None = None,            # NEW
    original_source_url_redacted: str | None = None,
) -> None:
```

SQL changes to `_UPSERT_REPORT_SQL`:
- Add `material_type`, `material_group`, `event_type` to INSERT column list and VALUES
- Add to ON CONFLICT UPDATE, moving as a unit with the other classification columns:

```sql
material_type = CASE
  WHEN EXCLUDED.classification IS NULL
    THEN {_SCHEMA}.reports.material_type
  WHEN {_SCHEMA}.reports.classification IS NULL
    THEN EXCLUDED.material_type
  WHEN COALESCE(EXCLUDED.classification_confidence, -1)
     > COALESCE({_SCHEMA}.reports.classification_confidence, -1)
    THEN EXCLUDED.material_type
  ELSE {_SCHEMA}.reports.material_type
END,
-- (same CASE pattern for material_group, event_type, AND reasoning)
```

All **six** classification columns move as a unit under the same confidence gate: `classification`, `classification_confidence`, `material_type`, `material_group`, `event_type`, `reasoning`. The `reasoning` column already exists (Spec 0004) and must be included in the atomic update set per AC22.

No new tests needed here — the existing upsert tests cover the pattern, and AC36 (integration test for mismatched pairing) tests the full write path.

## Phase 4: Crawler Integration

**ACs**: AC20, AC21

### `async_crawler.py`

In the PDF processing section (around line 425-510), after first_page_text extraction:

1. Import `get_taxonomy`, `classify_first_page_v2`, `ensure_loaded`
2. Add `taxonomy.ensure_loaded()` call in the crawler's startup/init path (before the work loop begins) for fail-fast semantics (AC10)
3. Replace `classify_first_page()` call with `classify_first_page_v2()` passing `taxonomy=get_taxonomy()`
4. Pass `material_type`, `material_group`, `event_type` from `ClassificationResult` to the upsert request

```python
taxonomy = get_taxonomy()
result = classify_first_page_v2(
    first_page_text, client=client, taxonomy=taxonomy,
    raise_on_error=False,
)
# ... existing error handling ...
# Pass to upsert:
material_type=result.material_type,
material_group=result.material_group,
event_type=result.event_type,
```

### `crawler.py` (sync)

Same change in the sync crawler's classification path (around line 366-453). Identical pattern for parity, including `taxonomy.ensure_loaded()` at startup.

### `async_db_writer.py`

Add `material_type`, `material_group`, `event_type` to `UpsertReportRequest` dataclass. Pass through to `upsert_report()` in `_do_single_write()`.

## Phase 5: classify_null Update

**ACs**: AC23

Update `lavandula/reports/tools/classify_null.py`:

1. Import `get_taxonomy`, `classify_first_page_v2`, `ensure_loaded`
2. Add `taxonomy.ensure_loaded()` in the tool's `main()` before entering the ThreadPoolExecutor work loop (fail-fast at CLI startup)
3. In `_classify_one()`, replace `classify_first_page()` with `classify_first_page_v2(taxonomy=get_taxonomy())`
4. In `_write_result()`, add `material_type`, `material_group`, `event_type` to the UPDATE statement
4. Add `--backfill-material-type` mode:
   - Different SQL query: `material_type IS NULL AND first_page_text IS NOT NULL AND classification IS NOT NULL`
   - Same classify + write logic (writes all 6 columns)
   - Mutually exclusive with the default null-classification mode (argparse `add_mutually_exclusive_group`)
   - Rate limiting: reuses the existing `--max-workers` ThreadPoolExecutor pattern (AC27). Default 4 workers, same as retry-null mode. Budget ledger integration preserved.

The `_write_result` function becomes:

```python
def _write_result(sha: str, result) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE lava_impact.reports SET "
                "  classification = :class, "
                "  classification_confidence = :conf, "
                "  material_type = :mt, "
                "  material_group = :mg, "
                "  event_type = :et, "
                "  classifier_model = :model, "
                "  classifier_version = :cver, "
                "  classified_at = :ts "
                "WHERE content_sha256 = :sha"
            ),
            {
                "class": result.classification,
                "conf": result.classification_confidence,
                "mt": result.material_type,
                "mg": result.material_group,
                "et": result.event_type,
                "model": _effective_classifier_model(sample_client, result),
                "cver": 2,
                "ts": iso_now(),
                "sha": sha,
            },
        )
```

**Tests** (`test_classify_null_v2.py`):
- `test_backfill_mode_selects_correct_rows` — only rows with classification!=NULL and material_type=NULL
- `test_backfill_writes_all_columns` — verifies material_type, material_group, event_type, classification, confidence all written
- `test_backfill_skips_already_typed` — rows with material_type!=NULL not selected
- `test_null_mode_uses_v2_schema` — default mode now writes v2 columns too

## Phase 6: Migration 007

**ACs**: AC1, AC2, AC3, AC4, AC5, AC6, AC28

Create `lavandula/migrations/rds/007_classifier_expansion.sql`:

```sql
-- Migration: 007_classifier_expansion
-- Date: 2026-04-26
-- Spec: 0023 (Classifier Expansion - Full Taxonomy Labels)
-- Target: PostgreSQL (RDS lava_prod1), schema lava_impact

BEGIN;

-- Show current state
DO $before$
BEGIN
  RAISE NOTICE '------ BEFORE ------';
  RAISE NOTICE 'reports columns: %', (
    SELECT string_agg(column_name, ', ' ORDER BY ordinal_position)
    FROM information_schema.columns
    WHERE table_schema = 'lava_impact' AND table_name = 'reports'
  );
END $before$;

-- Add columns
ALTER TABLE lava_impact.reports ADD COLUMN IF NOT EXISTS material_type TEXT;
ALTER TABLE lava_impact.reports ADD COLUMN IF NOT EXISTS material_group TEXT;
ALTER TABLE lava_impact.reports ADD COLUMN IF NOT EXISTS event_type TEXT;

-- CHECK constraints (derived from collateral_taxonomy.yaml)
ALTER TABLE lava_impact.reports ADD CONSTRAINT reports_mt_chk
  CHECK (material_type IS NULL OR material_type IN (
    -- (full list from spec, generated by validate_taxonomy_check.py)
  ));

ALTER TABLE lava_impact.reports ADD CONSTRAINT reports_mg_chk
  CHECK (material_group IS NULL OR material_group IN (
    'appeals','auction','campaign','day_of_event','invitations',
    'major_gifts','membership','other','peer_to_peer','periodic',
    'planned_giving','program_services','programs_journals','reports',
    'sector_specific','sponsorship','stewardship'
  ));

ALTER TABLE lava_impact.reports ADD CONSTRAINT reports_et_chk
  CHECK (event_type IS NULL OR event_type IN (
    'auction_event','ball','benefit_event','breakfast_fundraiser',
    'cocktail_reception','derby_polo_regatta','dinner_fundraiser',
    'fashion_show','food_wine_event','gala','golf_tournament',
    'luncheon','radiothon','ride_event','telethon','walk_run_event'
  ));

-- Indexes
CREATE INDEX IF NOT EXISTS idx_reports_material_type ON lava_impact.reports(material_type);
CREATE INDEX IF NOT EXISTS idx_reports_material_group ON lava_impact.reports(material_group);
CREATE INDEX IF NOT EXISTS idx_reports_event_type ON lava_impact.reports(event_type)
  WHERE event_type IS NOT NULL;

-- Update reports_public view
CREATE OR REPLACE VIEW lava_impact.reports_public AS
  SELECT content_sha256, source_org_ein, hosting_platform,
         attribution_confidence,
         archived_at, file_size_bytes, page_count,
         classification, classification_confidence,
         material_type, material_group, event_type,
         report_year, report_year_source,
         pdf_has_javascript, pdf_has_launch, pdf_has_embedded
  FROM lava_impact.reports
  WHERE attribution_confidence IN ('own_domain','platform_verified','wayback_archive')
    AND (
      (material_type IS NOT NULL AND material_type != 'not_relevant')
      OR
      (material_type IS NULL AND classification IS NOT NULL AND classification != 'not_a_report')
    )
    AND COALESCE(classification_confidence, 0) >= 0.8
    AND pdf_has_javascript = 0
    AND pdf_has_launch = 0
    AND pdf_has_embedded = 0;

-- Show result
DO $after$
BEGIN
  RAISE NOTICE '------ AFTER ------';
  RAISE NOTICE 'reports columns: %', (
    SELECT string_agg(column_name, ', ' ORDER BY ordinal_position)
    FROM information_schema.columns
    WHERE table_schema = 'lava_impact' AND table_name = 'reports'
  );
END $after$;

COMMIT;
```

**CHECK constraint generation workflow:**

1. The builder runs `validate_taxonomy_check.py --generate` which reads `collateral_taxonomy.yaml` and outputs the sorted SQL literal lists for `reports_mt_chk`, `reports_mg_chk`, and `reports_et_chk`.
2. The builder pastes the generated lists into `007_classifier_expansion.sql`.
3. The builder runs `validate_taxonomy_check.py --validate` which re-reads both files and confirms bidirectional match (AC28).
4. The drift tests (`test_drift.py`) also run this validation as part of the test suite, catching any future YAML edits that aren't reflected in the migration.

**Validation script** (`validate_taxonomy_check.py`):
- `--generate` mode: reads YAML, prints sorted SQL IN-list literals to stdout
- `--validate` mode: reads YAML + migration SQL, compares bidirectionally
- Exit code 0 on match, 1 on drift with diff output
- Supports reading from stdin for CI pipeline integration

**CI enforcement**: The test suite MUST include a test that runs `validate_taxonomy_check.py --validate` (or calls its validation logic directly) as part of the standard `pytest` run. This is already covered by the drift tests in `test_drift.py` (AC28, AC33), which perform the same bidirectional comparison. The drift tests serve as the CI gate — any YAML edit that adds/removes IDs without a matching migration update causes a test failure in the normal CI pipeline. No separate CI job or Makefile target is needed; `pytest` is the enforcement mechanism.

## Phase 7: Integration Tests

**ACs**: AC20, AC21, AC31, AC36, AC38

Integration test file exercising the full path through both crawler paths:

- `test_classify_annual_report_e2e` — mock classifier returns `annual_report`, verify all 6 columns in DB (AC31)
- `test_mismatched_group_rejected` — application validator catches `annual_report` + `auction` pairing (AC36)
- `test_reports_public_includes_v2_collateral` — insert a `sponsor_prospectus` row, verify it appears in view (AC38)
- `test_reports_public_includes_legacy_row` — insert a legacy row (material_type=NULL, classification='annual'), verify it appears in view (AC38)
- `test_async_crawler_passes_v2_fields` — mock async crawler classify path, verify `UpsertReportRequest` includes material_type/material_group/event_type (AC20)
- `test_sync_crawler_passes_v2_fields` — same for sync crawler path (AC21)
- `test_v2_error_preserves_retry_path` — when v2 validation fails (unknown material_type), result has classification=None and error set, matching AC16.2 retry semantics (AC19)
- `test_upsert_on_conflict_atomicity` — insert a row with v1 classification (no material_type), then upsert with higher-confidence v2 result. Verify all six columns (classification, classification_confidence, material_type, material_group, event_type, reasoning) updated atomically — no row should have v2 material_type with v1 reasoning or vice versa. (AC22)

## Dependency Order

```
Phase 1 (taxonomy.py) — no dependencies
Phase 2 (classify.py) — depends on Phase 1
Phase 3 (db_writer.py) — no dependencies (additive params)
Phase 4 (crawlers) — depends on Phases 1, 2, 3
Phase 5 (classify_null) — depends on Phases 1, 2
Phase 6 (migration) — no code dependencies, must apply before Phase 4/5 run against RDS
Phase 7 (integration tests) — depends on all phases
```

Phases 1+3 can be built in parallel. Phase 2 follows Phase 1. Phases 4+5 follow Phase 2. Phase 6 is SQL-only. Phase 7 is last.

## Test Strategy

| Test Type | Count | Phase |
|-----------|-------|-------|
| Taxonomy loader unit tests | ~14 | 1 |
| Classifier v2 unit tests | ~11 | 2 |
| Drift tests | ~4 | 2 |
| classify_null v2 tests | ~4 | 5 |
| Integration tests | ~8 | 7 |
| **Total** | **~41** | |

All tests use mocked classifier responses (no real API calls). The drift tests read the real YAML and migration SQL files.

## Rollout Order

**Migration 007 MUST be applied before the new code runs against RDS.** The new code writes `material_type`/`material_group`/`event_type` columns that don't exist until the migration runs. The runtime guard (AC19) handles the case where the LLM returns an unknown type, but it cannot handle missing columns.

1. **Apply Migration 007 to RDS** — adds columns, CHECK constraints, indexes, and view update. Safe to apply while the old code is still running (new columns are nullable, old code ignores them).
2. **Merge and deploy code** (Phases 1-5, 7) — new code writes v2 columns. Old rows unaffected.
3. **Restart crawler** — new PDFs get v2 classification from the start.
4. **Run `--backfill-material-type`** — reclassify existing v1-classified rows with v2 schema.
5. **Run `--retry-null-classifications`** — classify remaining NULL rows with v2 schema.

## Implementation Notes

**V2 error handling must preserve AC16.2 retry semantics.** When `_validate_tool_input_v2` raises `ClassifierError` (unknown material_type, unknown event_type, confidence out of range), `classify_first_page_v2` with `raise_on_error=False` must return a `ClassificationResult` with `classification=None` and `error` set — exactly as v1 does today. This ensures the existing retry-null pipeline picks up these rows on the next `--retry-null-classifications` run. The runtime guard (AC19) is implemented via this path, not by silently dropping the row.

## Consultation Log

### Round 1: Plan Review (2026-04-26)

**Codex** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

6 findings, all addressed in v2:

1. **Phase 3 missing `reasoning` in atomic update** — Fixed: explicitly listed all six columns (classification, classification_confidence, material_type, material_group, event_type, reasoning) in the atomic update set.
2. **`async_db_writer.py` missing from file inventory** — Added to inventory table (Phase 4).
3. **Phase 5 missing explicit rate limiting for backfill** — Added: backfill reuses `--max-workers` ThreadPoolExecutor, default 4 workers, budget ledger preserved (AC27).
4. **Rollout order: code before migration is unsafe** — Reversed: migration 007 applies FIRST (safe with old code running), then code deploys. Explicit note that migration must precede code deployment.
5. **Phase 6 missing CHECK constraint generation workflow** — Added: `--generate` mode produces SQL literals from YAML, `--validate` mode confirms bidirectional match. Builder workflow: generate → paste → validate.
6. **Phase 7 integration tests too narrow** — Added 3 more tests: async crawler v2 field passing (AC20), sync crawler parity (AC21), v2 error preserving retry path (AC19).

**Gemini** — Skipped (quota exhausted in spec review rounds).

### Round 2: Red-Team Security Review (2026-04-26)

**Codex** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

4 findings, all addressed in v3:

1. **`reports_mg_chk` missing `programs_journals`** — Already present in v2 plan (line 359). Verified: all 17 groups from spec's `_ALLOWED_GROUPS` are listed. No change needed.
2. **ON CONFLICT atomicity test missing** — Added `test_upsert_on_conflict_atomicity` to Phase 7: inserts v1 row, upserts with higher-confidence v2, verifies all six columns updated atomically (AC22).
3. **Startup taxonomy load not eagerly enforced** — Added `ensure_loaded()` function to taxonomy.py and explicit startup calls in async_crawler.py, crawler.py, and classify_null.py. Fail-fast at process start, not lazy on first classify call.
4. **CI enforcement of `validate_taxonomy_check.py --validate` not explicit** — Clarified: drift tests in `test_drift.py` (AC28, AC33) perform the same bidirectional YAML↔SQL comparison as part of the standard `pytest` run. No separate CI job needed; pytest IS the enforcement mechanism.

**Gemini** — Skipped (quota exhausted).

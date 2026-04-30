# Plan 0026: 990 Leadership & Contractor Intelligence

**Spec**: `locard/specs/0026-990-leadership-intelligence.md`
**Date**: 2026-04-30

## Overview

Extract leadership, key employee, and contractor data from IRS 990 XML filings (TEOS bulk download) into a `people` table. Includes Part VII Sections A & B and Schedule J compensation detail. 54 acceptance criteria.

## Implementation Phases

### Phase 1: Database Schema (Migration 010)

**File**: `lavandula/migrations/rds/010_990_people_filing_index.sql`

Create both tables in a single migration:

```sql
-- people table with all columns from spec (including Schedule J fields)
-- filing_index table with all columns (including filing_year, return_ts, is_amended)
-- All indexes from spec
```

Key details:
- `total_comp` is `GENERATED ALWAYS AS (...) STORED` — test this works on our RDS version
- `person_type` TEXT (not enum) — values: officer, director, key_employee, highest_compensated, contractor, listed
- `filing_year INTEGER NOT NULL` on filing_index

**ACs**: 1, 2, 3, 4

### Phase 2: XML Parser Module

**File**: `lavandula/nonprofits/irs990_parser.py`

Pure-function module — no DB, no HTTP. Takes XML bytes, returns structured data.

```python
@dataclass
class Person:
    person_name: str
    title: str | None
    person_type: str  # officer/director/key_employee/highest_compensated/listed
    avg_hours_per_week: Decimal | None
    reportable_comp: int | None   # cents
    related_org_comp: int | None  # cents
    other_comp: int | None        # cents
    services_desc: str | None
    is_officer: bool
    is_director: bool
    is_key_employee: bool
    is_highest_comp: bool
    is_former: bool
    # Schedule J fields (None if not in Schedule J)
    base_comp: int | None
    bonus: int | None
    other_reportable: int | None
    deferred_comp: int | None
    nontaxable_benefits: int | None
    total_comp_sch_j: int | None

@dataclass
class FilingMetadata:
    return_ts: datetime | None
    is_amended: bool
    ein: str
    tax_period: str

@dataclass
class ParseResult:
    metadata: FilingMetadata
    people: list[Person]
    warnings: list[str]

def parse_990_xml(xml_bytes: bytes) -> ParseResult:
    """Parse Part VII A, B, and Schedule J from a 990 XML filing."""
```

Implementation notes:
- Use `defusedxml.ElementTree` exclusively — `import defusedxml.ElementTree as ET`
- Namespace-agnostic parsing: use `local_name()` helper to strip namespace prefix, or iterate and match `tag.endswith('}Form990PartVIISectionAGrp')`
- Name normalization: XML entity decode (automatic via parser), then `re.sub(r'<[^>]+>', '', value)` for HTML strip, then `' '.join(value.split())` for whitespace collapse
- Boolean parsing: `_is_truthy(el)` → strip + case-insensitive check for X/true/1
- Compensation: `_cents(el)` → `int(el.text.strip()) * 100` if present, else None
- `person_type` derivation: check flags in priority order (officer > key_employee > highest_compensated > director), fallback to 'listed'
- Contractor names: check `ContractorName/BusinessName/BusinessNameLine1Txt` first, then `ContractorName/PersonNm`
- Schedule J merge: after building Part VII person list, iterate `IRS990ScheduleJ//RltdOrgOfficerTrstKeyEmplGrp`, match by normalized name to existing Person objects. On mismatch, add to warnings. If 100% mismatch, add ERROR-level warning.
- `ReturnTs` from `ReturnHeader/ReturnTs` — parse with `datetime.fromisoformat()`
- `AmendedReturnInd` from `ReturnData/IRS990/AmendedReturnInd` — truthy check
- Missing `PersonNm` → skip entry, add warning

**ACs**: 8, 9, 10, 11, 12, 13, 33, 37, 43, 44, 45, 49, 53

### Phase 3: Index Downloader

**File**: `lavandula/nonprofits/teos_index.py`

Downloads and filters TEOS index CSVs, inserts into `filing_index`.

```python
def download_and_filter_index(
    *,
    engine: Engine,
    year: int,
    state: str | None = None,
    ein: str | None = None,
) -> IndexStats:
    """Download TEOS index CSV for year, filter to our EINs, insert into filing_index."""
```

Implementation:
- Stream CSV via `requests.get(url, stream=True)` — 77MB+ files, don't load all into memory
- Use `csv.reader` on response iter_lines
- **EIN selection rules** (precedence):
  1. `--ein XXXXXXXXX` → single-EIN mode. Process only this EIN. Does NOT require it to be in `nonprofits_seed`. Bypasses `--state` filter entirely (if both given, `--ein` wins).
  2. `--state XX` → load all EINs from `nonprofits_seed WHERE state = :state` into a Python set for O(1) lookup.
  3. Neither `--state` nor `--ein` → **error**. CLI exits with "Must specify --state or --ein". We don't allow processing all 700K+ seeded EINs by accident.
- If `--limit` is set, truncate the EIN set to `--limit` entries (arbitrary order is fine — this is for testing).
- Filter CSV: `RETURN_TYPE = '990'` AND EIN in the selected set
- Insert with `ON CONFLICT (object_id) DO NOTHING` for idempotency
- `filing_year = year` (the TEOS directory year, not tax_period)
- `xml_batch_id` from CSV column 10

**ACs**: 5, 6, 7, 52

### Phase 4: Zip Downloader + Batch Processor

**File**: `lavandula/nonprofits/teos_download.py`

Downloads zips, extracts XML members, orchestrates parsing.

```python
def process_filings(
    *,
    engine: Engine,
    cache_dir: Path,
    skip_download: bool = False,
    reparse: bool = False,
    run_id: str,
    shutdown: ShutdownFlag | None = None,
) -> ProcessStats:
    """Group filings by batch, download zips, parse XMLs, upsert people."""
```

Implementation:
- Query `filing_index` grouped by `(filing_year, xml_batch_id)` — `ORDER BY filing_year, xml_batch_id`
- Status filter: `indexed` or `downloaded` (normal), or all statuses (if reparse)
- For `--reparse`: UPDATE filing_index SET status='downloaded', error_message=NULL, parsed_at=NULL WHERE status IN ('parsed', 'skipped', 'error') **AND scoped to the current run's EIN set and filing years**. The reset query must include `AND ein IN (:ein_set) AND filing_year IN (:years)` to avoid touching filings outside the operator's requested scope.
- **`--limit` enforcement**: `--limit` caps the number of **unique EINs** processed. In Phase 3, after loading matching EINs from `nonprofits_seed`, truncate the set to `--limit` EINs. Only those EINs' filings are inserted into `filing_index`. This means limit controls org count, not filing count (one org may have multiple filings across years).

- Per batch:
  1. Construct zip URL: `https://apps.irs.gov/pub/epostcard/990/xml/{filing_year}/{xml_batch_id}.zip` (note: `xml_batch_id` already includes the year prefix, e.g., `2024_TEOS_XML_01A`)
  2. Check cache: `cache_dir / f"{xml_batch_id}.zip"`
  3. If not cached and not skip_download: download atomically (`.tmp` suffix, rename on success, Content-Length check)
  4. If not cached and skip_download: log warning, leave filings as `indexed`, continue
  5. Open zip with `zipfile.ZipFile`, iterate over batch's object_ids:
     - Check `ZipInfo.file_size` < 50MB
     - Validate member name pattern
     - Read XML bytes
     - Call `parse_990_xml(xml_bytes)`
     - Upsert `people` rows
     - Update `filing_index` status
- Rate limit: `time.sleep(1.0)` between zip downloads (not between member reads within a zip)
- Retry: exponential backoff 2s/4s/8s for HTTP 429/5xx/ConnectionError, max 3 attempts
- 404: mark all filings in batch as error, continue
- Missing member in zip: mark that specific filing as error (AC48), continue others
- **Error message sanitization**: When setting `filing_index.error_message`, never persist raw XML content or Python stack traces. Use sanitized summaries only: `f"Part VII parse error: {type(exc).__name__}"` or `"Missing member {object_id}_public.xml in zip"`. Truncate to 500 chars max.

DB upsert pattern:
```python
INSERT INTO lava_corpus.people (ein, tax_period, object_id, person_name, ...)
VALUES (:ein, :tax_period, :object_id, :person_name, ...)
ON CONFLICT (ein, object_id, person_name, person_type) DO UPDATE SET
    title = EXCLUDED.title,
    reportable_comp = EXCLUDED.reportable_comp,
    ... all mutable fields ...
    extracted_at = NOW(),
    run_id = EXCLUDED.run_id
```

Schedule J UPDATE (after Part VII insert):
```python
UPDATE lava_corpus.people
SET base_comp = :base_comp, bonus = :bonus, ...
WHERE object_id = :object_id AND person_name = :person_name
```

**ACs**: 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 38, 39, 40, 41, 48, 50, 54

### Phase 5: CLI Entry Point

**File**: `lavandula/nonprofits/tools/enrich_990.py`

Follows the pattern of `seed_enumerate.py` and `pipeline_classify.py`.

```python
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(...)
    ap.add_argument("--state", ...)
    ap.add_argument("--years", ...)
    ap.add_argument("--limit", ...)
    ap.add_argument("--ein", ...)
    ap.add_argument("--cache-dir", ...)
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--reparse", action="store_true")
    args = ap.parse_args(argv)
    
    # Validate inputs
    # --ein: ^\d{9}$
    # --state: ^[A-Z]{2}$
    # --years: comma-separated 4-digit years
    # --cache-dir: existing directory, no symlinks
    
    # Generate run_id
    run_id = str(uuid4())
    
    # Step 1: Download + filter index
    for year in years:
        download_and_filter_index(engine=engine, year=year, state=state, ein=ein)
    
    # Step 2: Process filings
    process_filings(engine=engine, cache_dir=cache_dir, ...)
```

Default years: `list(range(current_year - 4, current_year + 1))`
Default cache dir: `~/.lavandula/990-cache/` (create if not exists, validate no symlink)
Log cache size at startup: `sum(f.stat().st_size for f in cache_dir.glob("*.zip"))`

**ACs**: 14, 42

### Phase 6: Dashboard Integration

**Files**:
- `lavandula/dashboard/pipeline/orchestrator.py` — add to COMMAND_MAP
- `lavandula/dashboard/pipeline/forms.py` — add form if needed
- `lavandula/dashboard/pipeline/templates/` — update form template

Add to COMMAND_MAP:
```python
"990-enrich": {
    "cmd": ["python3", "-m", "lavandula.nonprofits.tools.enrich_990"],
    "params": {
        "state": {"type": "choice", "choices": US_STATES, "flag": "--state"},
        "years": {"type": "text", "pattern": r"^\d{4}(,\d{4})*$", "flag": "--years"},
        "limit": {"type": "int", "min": 1, "max": 999999, "flag": "--limit"},
    },
},
```

Years field pre-populated with last 5 years. Server-side validation rejects malformed input.

**ACs**: 25, 26

### Phase 7: Tests

**Files**:
- `lavandula/nonprofits/tests/test_irs990_parser.py` — parser unit tests
- `lavandula/nonprofits/tests/test_teos_index.py` — index processing tests
- `lavandula/nonprofits/tests/test_teos_download.py` — download + batch processing tests
- `lavandula/nonprofits/tests/test_enrich_990.py` — CLI integration tests

**Test fixtures**: Use the two sample 990 XMLs already saved:
- `sample_pdfs/990-test/oneonta_cemetery_990.xml` — small org, Part VII A only, no Schedule J, no contractors
- `sample_pdfs/990-test/project_lead_the_way_990.xml` — large org, Part VII A + B, Schedule J, contractors, amended filing, former officer

Create additional minimal fixture XMLs for edge cases:
- `test_fixtures/990_no_part_vii.xml` — valid 990 with no Part VII section
- `test_fixtures/990_no_person_nm.xml` — Part VII entry missing PersonNm
- `test_fixtures/990_schedule_j_mismatch.xml` — Schedule J names don't match Part VII
- `test_fixtures/990_xxe_attack.xml` — DTD with external entity (must be rejected)
- `test_fixtures/malicious_zip.zip` — zip with path-traversing member name (`../../../etc/passwd`) — must be rejected by member name validation (AC34)

Parser tests (Phase 2 ACs):
- Part VII Section A parsing with all field variations
- Part VII Section B parsing (BusinessName vs PersonNm)
- Schedule J merge with matching names
- Schedule J 100% mismatch → ERROR warning
- Missing Part VII → empty people list
- Missing PersonNm → skip with warning
- Boolean indicator variations (X, x, true, TRUE, 1)
- Compensation cents conversion
- person_type priority derivation
- is_former with officer role
- No-flag entries → person_type='listed'
- XXE rejection via defusedxml
- HTML tag stripping

Index tests (Phase 3 ACs):
- Idempotent insertion (same year twice)
- Filter to RETURN_TYPE='990' only
- Filter to matching EINs

Download/batch tests (Phase 4 ACs):
- Batch grouping (mock zip, verify opened once)
- Atomic download (mock HTTP, verify tmp+rename)
- Missing member in zip → error status
- --skip-download with missing cache → warning
- --reparse resets error rows
- Retry on 429/5xx
- Zip bomb rejection (file_size > 50MB)
- Status lifecycle transitions

CLI tests (Phase 5 ACs):
- Input validation (--ein, --state, --years, --cache-dir)

**AC32 manual verification**: The integration test for real TEOS index download is NOT in the automated test suite. Builder runs it once manually during development: `python3 -c "from lavandula.nonprofits.teos_index import ...; ..."` against the live IRS endpoint, verifying the CSV has the expected 10 columns and filters work. Document the result in the PR description. Pass criteria: CSV downloads, header matches expected columns, at least 1 row matches a known test EIN.

**ACs**: 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 46, 47, 49, 50, 51, 52, 53, 54

## File Summary

| File | Action | Description |
|------|--------|-------------|
| `lavandula/migrations/rds/010_990_people_filing_index.sql` | CREATE | Migration: people + filing_index tables |
| `lavandula/nonprofits/irs990_parser.py` | CREATE | Pure XML parser (defusedxml, namespace-agnostic) |
| `lavandula/nonprofits/teos_index.py` | CREATE | TEOS index CSV download + filter + insert |
| `lavandula/nonprofits/teos_download.py` | CREATE | Zip download, batch processing, DB upsert |
| `lavandula/nonprofits/tools/enrich_990.py` | CREATE | CLI entry point |
| `lavandula/dashboard/pipeline/orchestrator.py` | MODIFY | Add 990-enrich to COMMAND_MAP |
| `lavandula/nonprofits/tests/test_irs990_parser.py` | CREATE | Parser unit tests |
| `lavandula/nonprofits/tests/test_teos_index.py` | CREATE | Index processing tests |
| `lavandula/nonprofits/tests/test_teos_download.py` | CREATE | Download + batch tests |
| `lavandula/nonprofits/tests/test_enrich_990.py` | CREATE | CLI integration tests |
| `lavandula/nonprofits/tests/fixtures/` | CREATE | Test fixture XML files |

## Dependencies

- `defusedxml` — **Phase 0 prerequisite**: `pip install defusedxml` and add to `requirements.txt` (or equivalent). Builder must verify import works before starting Phase 2. This is a hard dependency — the parser cannot use stdlib xml.etree.
- `requests` — already in use
- `sqlalchemy` — already in use
- No new infrastructure needed

## Build Order

Phases 1-5 are sequential (each depends on the previous). Phase 6 (dashboard) can be done in parallel with Phase 7 (tests). Recommended:

1. Phase 1 (migration) — quick, unblocks everything
2. Phase 2 (parser) — core logic, most complex, test with fixtures immediately
3. Phase 7 parser tests — write alongside Phase 2 (TDD)
4. Phase 3 (index) — depends on Phase 1 schema
5. Phase 4 (download/batch) — depends on Phase 2 + 3
6. Phase 7 remaining tests — write alongside Phases 3-4
7. Phase 5 (CLI) — thin wrapper
8. Phase 6 (dashboard) — thin wrapper

## Estimated Effort

- Phase 1: ~30 min (SQL migration)
- Phase 2: ~3 hours (parser is the bulk of the work — namespace handling, Schedule J merge)
- Phase 3: ~1 hour (streaming CSV, EIN set lookup)
- Phase 4: ~2 hours (batch grouping, atomic download, retry, upsert)
- Phase 5: ~30 min (CLI argparse)
- Phase 6: ~30 min (COMMAND_MAP entry)
- Phase 7: ~3 hours (fixtures + tests)

**Total**: ~10 hours for a single builder

## Risks

1. **IRS XML namespace variations across years** — Different schema versions (2019v5.0, 2023v4.0, etc.) may use different namespaces. The parser must be namespace-agnostic. Test with real XMLs from multiple years.
2. **Large index CSV (77MB+)** — Must stream, not load into memory. `requests.get(stream=True)` + `csv.reader`.
3. **Schedule J name matching** — Names may have minor differences between Part VII and Schedule J (e.g., suffix differences). V1 uses exact match after normalization. Monitor mismatch rate in production.
4. **defusedxml not installed** — Builder must add to requirements/dependencies.

## Consultation Log

### Round 1: Plan Review (2026-04-30)

**Codex** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

5 findings, all addressed:

1. **`--limit` behavior undefined** — Fixed: `--limit` caps unique EINs in Phase 3 (index filtering). One org may have multiple filings.
2. **Zip URL construction inconsistent with spec** — Clarified: `xml_batch_id` already includes the year prefix (e.g., `2024_TEOS_XML_01A`), so URL is `{filing_year}/{xml_batch_id}.zip`. Also fixed the spec's wording.
3. **`defusedxml` dependency not assigned to a phase** — Fixed: added as Phase 0 prerequisite with explicit install + verify step.
4. **AC32 integration test undefined** — Fixed: documented as manual verification step with pass criteria.
5. **Error message sanitization not explicit** — Fixed: added sanitization rule in Phase 4 (no raw XML, no stack traces, 500 char max).

**Gemini** — Quota exhausted, no response.

### Round 2: Red Team Security Review (2026-04-30)

**Codex** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

4 findings, all addressed:

1. **`--ein` semantics unspecified** — Fixed: `--ein` bypasses `--state`, doesn't require `nonprofits_seed`. Neither `--state` nor `--ein` → error exit.
2. **`--reparse` scope too broad** — Fixed: reset query scoped to current EIN set + filing years.
3. **Missing path-traversal zip test** — Fixed: added `malicious_zip.zip` test fixture for AC34.
4. **Default scope ambiguous** — Fixed: neither `--state` nor `--ein` is an error.

**Gemini** — Quota exhausted, no response.

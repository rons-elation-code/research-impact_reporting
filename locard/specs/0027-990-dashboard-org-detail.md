# Spec 0027: 990 Dashboard — Org Detail View & Pipeline Controls

**Status:** Draft  
**Priority:** High  
**Dependencies:** Spec 0019 (Dashboard), Spec 0026 (990 Leadership Intelligence)  
**Author:** Architect  
**Date:** 2026-05-01

---

## Problem Statement

Spec 0026 built the 990 leadership extraction pipeline (TEOS index download → XML parse → `people` table), but there is no way to:
1. **Trigger** the pipeline from the dashboard — operators must SSH in and run CLI commands
2. **View** the extracted leadership data — the org detail page only shows seed/resolver fields

The pipeline has two distinct phases with different operational profiles:
- **Index Download**: Fast (~4s/year), lightweight, safe to re-run freely
- **Parse/Import**: Slow (minutes to hours depending on scope), downloads 68-500MB zip files, heavy I/O

These should be separate controls because operators will re-index frequently (to pick up new filings) but only parse when they have unprocessed filings.

## Goals

### Goal 1: Org Detail Enhancement — Leadership & Compensation

Enhance the existing org detail page (`/dashboard/orgs/<ein>/`) to display 990 leadership intelligence from the `lava_corpus.people` and `lava_corpus.filing_index` tables.

**1a. Filing Summary Section**
- Show all filings for this EIN from `filing_index`
- Display: tax period, return type, filing year, status, whether amended
- Sort by `tax_period DESC, object_id DESC` (most recent first; for same tax period, higher object_id = later filing)

**1b. Filing Picker**
- Dropdown at the top of the leadership section to select which filing's people to display
- Defaults to the first filing in the sorted list (most recent tax period, latest object_id)
- Changing the picker reloads the people tables for that filing (full page reload with `?filing=<object_id>` query param)

**1c. Officers & Directors Section**
- Table of non-contractor people from the selected filing
- Columns: Name, Title, Type (officer/director/key_employee/highest_compensated), Reportable Comp, Schedule J Total
- Color-code person_type (officer=blue, director=gray, key_employee=green, highest_compensated=amber)
- Sort by reportable_comp descending (highest paid first), then person_name ASC for zero-comp entries

**1d. Top Contractors Section**
- Table of contractor-type people from the selected filing
- Columns: Name, Services Description, Compensation
- Sort by compensation descending

**1e. Schedule J Compensation Detail**
- Expandable section (collapsed by default) showing full Schedule J breakdown
- Only for people where `total_comp_sch_j IS NOT NULL`
- Columns: Name, Base, Bonus, Other Reportable, Deferred, Nontaxable Benefits, Total

**1f. Cross-Filing Comparison**
- When multiple filings exist, show a "Compare Across Filings" toggle
- Matches people across filings by exact `person_name` string (case-sensitive)
- Displays a table with rows = unique person names, columns = filings (by tax period)
- Cell values = reportable_comp for that person in that filing. Missing = "—"
- Scoped to a single EIN (the current org detail page)
- Known limitation: name variants across filings (e.g. "DAVID DIMMETT ED D" vs "DAVID DIMMETT ED") will appear as separate rows. This is acceptable for V1.

### Goal 2: TEOS Index Pipeline Control

New pipeline control page at `/dashboard/990-index/` for downloading and filtering the TEOS index CSV.

**Form Fields:**
| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| State | Dropdown (US states) | No* | — | Filters EINs from nonprofits_seed |
| EIN | Text (9 digits) | No* | — | Single EIN mode |
| Years | Text (comma-separated) | Yes | Current year | e.g. "2023,2024" |

*One of State or EIN is required (same validation as CLI).

**Behavior:**
- Submits as a Job (queued, tracked, cancellable via existing Job infrastructure)
- Maps to orchestrator phase `990-index` (new COMMAND_MAP entry)
- The CLI needs a `--index-only` flag added to `enrich_990.py` to run only the index download phase
- Duplicate prevention: only one `990-index` job can be pending/running at a time (existing `DuplicateJobError` pattern)
- Years outside TEOS availability (pre-2019 or future): the IRS returns HTTP 404, CLI logs warning and continues to next year. No special dashboard handling needed.

**Status Display:**
- Count of `filing_index` rows by status for the currently selected state/EIN scope
- Derived from `filing_index` table aggregation — no separate run metadata table

### Goal 3: 990 XML Parse/Import Pipeline Control

New pipeline control page at `/dashboard/990-parse/` for downloading zip files and parsing 990 XMLs.

**Form Fields:**
| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| State | Dropdown (US states) | No* | — | Scope to state's EINs |
| EIN | Text (9 digits) | No* | — | Single EIN mode |
| Years | Text (comma-separated) | Yes | Current year | e.g. "2023,2024" |
| Limit | Integer | No | — | Max unique EINs to process |
| Skip Download | Checkbox | No | false | Use cached zips only |
| Reparse | Checkbox | No | false | Re-process error rows |

*One of State or EIN is required.

**Behavior:**
- Submits as a Job (queued, tracked, cancellable via existing Job infrastructure)
- Maps to orchestrator phase `990-parse` (new COMMAND_MAP entry)
- The CLI needs a `--parse-only` flag added to `enrich_990.py` to skip the index download phase
- Duplicate prevention: only one `990-parse` job can be pending/running at a time
- Parse-only with no indexed filings: job completes successfully with "0 filings processed" in output (no-op, not an error)

**Status Display:**
- Count of filings by status (indexed/downloaded/parsed/error) from `filing_index` aggregation
- People count from `people` table aggregation for the selected scope
- Cache status (number of cached zip files, total size on disk) from filesystem scan of `~/.lavandula/990-cache/`

## Filing Status Semantics

The `filing_index.status` column tracks each filing's lifecycle. States are **mutually exclusive and terminal until re-processed**:

| Status | Meaning | Transition |
|--------|---------|------------|
| `indexed` | Row inserted from TEOS CSV. Zip not yet downloaded. | Set by index download |
| `downloaded` | Zip containing this filing has been cached locally. XML not yet parsed. | Set when zip download completes |
| `parsed` | XML successfully parsed, people rows upserted. | Set after successful parse |
| `error` | Parse or download failed. `error_message` column has details. | Set on failure; `--reparse` resets to `downloaded` |

Progression: `indexed → downloaded → parsed` (happy path) or `indexed → downloaded → error` (failure).

## Non-Goals

- **Contractor enrichment** — Spec 0028 handles AI-powered contractor descriptions
- **Editing people data** — Read-only display
- **990EZ/990PF parsing** — Spec 0026 scoped to full 990 only
- **Role-based access control** — This is a single-operator system (see Security). All authenticated users have full access.
- **Real-time progress streaming** — Job progress is visible via the existing job log view (stdout capture). No WebSocket or polling needed beyond what Spec 0019 already provides.

## Technical Implementation

### Architecture

The implementation follows established dashboard patterns from Spec 0019:

```
Forms (forms.py)           →  Views (views.py)           →  Templates
  EnrichIndexForm                EnrichIndexView               990_index.html
  EnrichParseForm                EnrichParseView               990_parse.html
                                 OrgDetailView (enhanced)       org_detail.html (enhanced)

Orchestrator (orchestrator.py)
  COMMAND_MAP["990-index"]  →  enrich_990 --index-only
  COMMAND_MAP["990-parse"]  →  enrich_990 --parse-only
```

### CLI Changes (enrich_990.py)

Add two mutually exclusive flags:
- `--index-only`: Run only `download_and_filter_index()` for each year, skip `process_filings()`
- `--parse-only`: Skip `download_and_filter_index()`, run only `process_filings()`
- Neither flag: Run both (current behavior, unchanged)
- Both flags simultaneously: argparse error (mutually exclusive group)

### Unmanaged Models

Add read-only unmanaged Django models for query convenience. These route to the `pipeline` database alias via the existing `PipelineRouter`.

```python
class FilingIndex(models.Model):
    object_id = models.CharField(primary_key=True, max_length=30)
    ein = models.CharField(max_length=9)
    tax_period = models.CharField(max_length=6)
    return_type = models.CharField(max_length=10)
    filing_year = models.IntegerField()
    status = models.CharField(max_length=20)
    taxpayer_name = models.CharField(max_length=200, null=True)
    xml_batch_id = models.CharField(max_length=30, null=True)
    error_message = models.TextField(null=True)
    run_id = models.CharField(max_length=50, null=True)

    class Meta:
        managed = False
        db_table = '"lava_corpus"."filing_index"'

class Person(models.Model):
    id = models.AutoField(primary_key=True)
    ein = models.CharField(max_length=9)
    object_id = models.CharField(max_length=30)
    tax_period = models.CharField(max_length=6)
    person_name = models.CharField(max_length=200)
    title = models.CharField(max_length=200, null=True)
    person_type = models.CharField(max_length=30)
    reportable_comp = models.BigIntegerField(null=True)
    related_org_comp = models.BigIntegerField(null=True)
    other_comp = models.BigIntegerField(null=True)
    total_comp = models.BigIntegerField(null=True)
    base_comp = models.BigIntegerField(null=True)
    bonus = models.BigIntegerField(null=True)
    other_reportable = models.BigIntegerField(null=True)
    deferred_comp = models.BigIntegerField(null=True)
    nontaxable_benefits = models.BigIntegerField(null=True)
    total_comp_sch_j = models.BigIntegerField(null=True)
    services_desc = models.TextField(null=True)
    avg_hours_per_week = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    is_officer = models.BooleanField(default=False)
    is_director = models.BooleanField(default=False)
    is_key_employee = models.BooleanField(default=False)
    is_highest_comp = models.BooleanField(default=False)
    is_former = models.BooleanField(default=False)
    extracted_at = models.DateTimeField(null=True)
    run_id = models.CharField(max_length=50, null=True)

    class Meta:
        managed = False
        db_table = '"lava_corpus"."people"'
```

### Org Detail View Enhancement

The existing `OrgDetailView` at `/dashboard/orgs/<ein>/` needs additional context:

```python
def get_context_data(self, **kwargs):
    ctx = super().get_context_data(**kwargs)
    ein = self.object.ein

    filings = FilingIndex.objects.using("pipeline").filter(
        ein=ein
    ).order_by("-tax_period", "-object_id")
    ctx["filings"] = filings

    # Filing picker: use query param or default to first
    selected_oid = self.request.GET.get("filing")
    if selected_oid:
        selected = filings.filter(object_id=selected_oid).first()
    else:
        selected = filings.first()
    ctx["selected_filing"] = selected

    if selected:
        people_qs = Person.objects.using("pipeline").filter(
            ein=ein, object_id=selected.object_id
        )
        ctx["officers"] = people_qs.exclude(
            person_type="contractor"
        ).order_by("-reportable_comp", "person_name")
        ctx["contractors"] = people_qs.filter(
            person_type="contractor"
        ).order_by("-reportable_comp")
        ctx["schedule_j"] = people_qs.filter(
            total_comp_sch_j__isnull=False
        ).order_by("-total_comp_sch_j")
    return ctx
```

### Navigation

Add to the sidebar in `base.html`:
- **990 Index** link → `/dashboard/990-index/`
- **990 Parse** link → `/dashboard/990-parse/`

### Currency Formatting

All compensation amounts are whole dollars (BIGINT in DB). Display with `$` prefix and comma grouping. Use a custom template filter:

```python
@register.filter
def currency(value):
    if value is None:
        return "—"
    return f"${value:,}"
```

### Cancellation Behavior

Job cancellation uses the existing Spec 0019 infrastructure:
- **Queued jobs**: Status set to `cancelled`, never started
- **Running jobs**: `SIGTERM` sent to subprocess (via `process_manager.stop_process()`), 10s grace period, then `SIGKILL`
- The `enrich_990` CLI already handles `SIGTERM` gracefully (sets a shutdown event, completes current filing, exits)

### Error Display

Error messages from parse/import are already sanitized by `_sanitize_error()` in `teos_download.py` (no raw XML, no stack traces, 500 char max). The dashboard displays `filing_index.error_message` as-is since it's pre-sanitized.

## Acceptance Criteria

### Org Detail Enhancement
- **AC1**: Org detail page shows "Filings" section with all filing_index rows for this EIN
- **AC2**: Filings sorted by `tax_period DESC, object_id DESC`
- **AC3**: Filing status displayed with color coding (parsed=green, error=red, downloaded=blue, indexed=gray)
- **AC4**: Filing picker dropdown defaults to most recent filing (first in sort order)
- **AC5**: Selecting a different filing via picker reloads people tables (`?filing=<object_id>`)
- **AC6**: "Officers & Directors" table shows non-contractor people from selected filing
- **AC7**: Officers table sorted by reportable_comp DESC, then person_name ASC
- **AC8**: Person type color-coded (officer=blue, director=gray, key_employee=green, highest_compensated=amber)
- **AC9**: "Top Contractors" table shows contractor-type people from selected filing
- **AC10**: Contractors sorted by compensation DESC
- **AC11**: "Schedule J Detail" section collapsed by default, expandable
- **AC12**: Schedule J only shows people where total_comp_sch_j IS NOT NULL
- **AC13**: All compensation values formatted as currency ($X,XXX) with null → "—"
- **AC14**: Org detail page renders correctly for orgs with zero filings (graceful empty state: "No 990 filings found")
- **AC15**: Org detail page renders correctly for filings with zero people ("No leadership data extracted")
- **AC16**: Cross-filing comparison matches people by exact `person_name` (case-sensitive)
- **AC17**: Comparison table: rows = unique person names, columns = filings by tax_period, cells = reportable_comp or "—"
- **AC18**: Comparison scoped to single EIN (current org detail page only)

### 990 Index Pipeline Control
- **AC19**: Pipeline control page at `/dashboard/990-index/`
- **AC20**: Form requires either State or EIN (server-side validation, form error if both blank)
- **AC21**: Years field validates as comma-separated 4-digit years (regex: `^\d{4}(\s*,\s*\d{4})*$`)
- **AC22**: Form submission creates a Job with phase="990-index"
- **AC23**: Job maps to `enrich_990 --index-only --state XX --years YYYY` (or `--ein` for single EIN)
- **AC24**: `--index-only` flag runs only index download, skips parse/import
- **AC25**: Status section shows filing_index row counts grouped by status
- **AC26**: Sidebar includes "990 Index" link
- **AC27**: Duplicate job prevention: error message if 990-index job already pending/running

### 990 Parse/Import Pipeline Control
- **AC28**: Pipeline control page at `/dashboard/990-parse/`
- **AC29**: Form requires either State or EIN
- **AC30**: Skip Download and Reparse checkboxes map to `--skip-download` and `--reparse` flags
- **AC31**: Form submission creates a Job with phase="990-parse"
- **AC32**: Job maps to `enrich_990 --parse-only --state XX --years YYYY [--skip-download] [--reparse] [--limit N]`
- **AC33**: `--parse-only` flag skips index download, runs only parse/import
- **AC34**: Parse-only with zero indexed filings: job completes successfully, logs "0 filings processed"
- **AC35**: Status section shows filings by status + people count + error count
- **AC36**: Sidebar includes "990 Parse" link
- **AC37**: Duplicate job prevention: error message if 990-parse job already pending/running

### Infrastructure
- **AC38**: Two new COMMAND_MAP entries: `990-index` and `990-parse`
- **AC39**: Unmanaged Django models for FilingIndex and Person with all columns from migration 010
- **AC40**: Models route to `pipeline` database alias via existing PipelineRouter
- **AC41**: Currency template filter formats BIGINT as $X,XXX with null → "—"
- **AC42**: All new views require login (LoginRequiredMixin)
- **AC43**: Audit logging for job creation (same `_log_audit()` pattern as existing pipeline controls)
- **AC44**: `--index-only` and `--parse-only` are mutually exclusive (argparse error if both specified)

## Security Considerations

- **Single-operator system**: This dashboard is used by a single operator (ronp). There is no multi-tenant access control or role-based authorization. All authenticated users have identical access. This is consistent with Spec 0019 and the project's single-operator DB architecture (see project memory).
- **Read-only data access**: People/filing data displayed via unmanaged models with read-only database routing (`PipelineRouter` blocks writes to unmanaged models). No writes to pipeline DB from dashboard views.
- **Job launch controls**: Duplicate prevention (one pending/running job per phase) prevents accidental repeated submissions. The existing Job queue infrastructure handles concurrency.
- **Input validation**: EIN validated as exactly 9 digits (regex). Years validated as 4-digit integers in range [2019, current_year]. State validated against US_STATES whitelist. All validation server-side (forms.py).
- **Error message sanitization**: Error messages displayed in filing status are pre-sanitized by `_sanitize_error()` (no raw XML, no stack traces, 500 char max). No additional sanitization needed in dashboard.
- **No PII exposure**: 990 data is public record (IRS publishes it via TEOS). Names and compensation are already publicly available.
- **SQL injection**: All queries via Django ORM (unmanaged models) with parameterized queries. No string interpolation in SQL.

## Testing Requirements

- **Unit tests**: Form validation (EIN format, years format, state-or-ein required, mutually exclusive flags)
- **View tests**: Each view returns 200 with expected context keys, 302 for unauthenticated
- **Template tests**: Currency filter formatting ($X,XXX and null → "—"), empty state rendering
- **Integration tests**: Job creation flow for both pipeline phases, duplicate prevention
- **CLI tests**: `--index-only` and `--parse-only` flags work correctly, both-specified raises error
- **Edge case tests**: Parse-only with no indexed filings (no-op), index-only with zero matches (success with 0 rows), org detail with zero filings, org detail with zero people, cross-filing comparison with name variants

## Decisions (from Open Questions)

1. **Separate sidebar entries** for 990 Index and 990 Parse. They have different operational profiles and form fields. A combined page with tabs adds complexity without benefit.
2. **Filing picker dropdown** on org detail page. Default to most recent filing, allow selecting any filing via `?filing=<object_id>` query param. Cross-filing comparison is a separate toggle that shows all filings side-by-side.

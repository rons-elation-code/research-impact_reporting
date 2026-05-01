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
- Display: tax period, return type, filing year, status (indexed/downloaded/parsed/error), whether amended
- Sort by tax period descending (most recent first)

**1b. Officers & Directors Section**
- Table of non-contractor people from the most recent filing
- Columns: Name, Title, Type (officer/director/key_employee/highest_compensated), Reportable Comp, Schedule J Total
- Color-code person_type (officer=blue, director=gray, key_employee=green, highest_compensated=amber)
- Sort by reportable_comp descending (highest paid first), then alphabetically for zero-comp entries

**1c. Top Contractors Section**
- Table of contractor-type people from the most recent filing
- Columns: Name, Services Description, Compensation
- Sort by compensation descending

**1d. Schedule J Compensation Detail**
- Expandable section (collapsed by default) showing full Schedule J breakdown
- Only for people where `total_comp_sch_j IS NOT NULL`
- Columns: Name, Base, Bonus, Other Reportable, Deferred, Nontaxable Benefits, Total

**1e. Cross-Filing Comparison**
- When multiple filings exist, show a "Compare" toggle that displays compensation for the same person across filings
- Helps identify tenure and compensation trends

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
- Submits as a Job (queued, tracked, cancellable)
- Maps to orchestrator phase `990-index` (new COMMAND_MAP entry)
- The CLI needs a `--index-only` flag added to `enrich_990.py` to run only the index download phase

**Status Display:**
- Count of `filing_index` rows by status (indexed/downloaded/parsed/error) for the selected scope
- Most recent index download run (timestamp, rows matched)

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
- Submits as a Job (queued, tracked, cancellable)
- Maps to orchestrator phase `990-parse` (new COMMAND_MAP entry)
- The CLI needs a `--parse-only` flag added to `enrich_990.py` to skip the index download phase

**Status Display:**
- Count of filings by status (indexed/downloaded/parsed/error)
- Current run progress (filings processed / total, people extracted, errors)
- Cache status (number of cached zip files, total size)

## Non-Goals

- **Contractor enrichment** — Spec 0028 handles AI-powered contractor descriptions
- **New Django models for people/filing_index** — Use raw SQL queries via the existing unmanaged model pattern or direct `connection.cursor()` calls against the pipeline database
- **Editing people data** — This is read-only display
- **990EZ/990PF parsing** — Spec 0026 scoped to full 990 only
- **Filing amendment resolution** — Display all filings; don't auto-select "most current"

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

### Unmanaged Models

Add read-only unmanaged Django models for query convenience:

```python
class FilingIndex(models.Model):
    object_id = models.CharField(primary_key=True)
    ein = models.CharField()
    tax_period = models.CharField()
    filing_year = models.IntegerField()
    status = models.CharField()
    # ...
    class Meta:
        managed = False
        db_table = '"lava_corpus"."filing_index"'

class Person(models.Model):
    ein = models.CharField()
    person_name = models.CharField()
    title = models.CharField()
    person_type = models.CharField()
    reportable_comp = models.BigIntegerField()
    # ...
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
    ctx["filings"] = FilingIndex.objects.using("pipeline").filter(ein=ein).order_by("-tax_period")
    ctx["latest_filing"] = ctx["filings"].first()
    if ctx["latest_filing"]:
        obj_id = ctx["latest_filing"].object_id
        people_qs = Person.objects.using("pipeline").filter(ein=ein, object_id=obj_id)
        ctx["officers"] = people_qs.exclude(person_type="contractor").order_by("-reportable_comp", "person_name")
        ctx["contractors"] = people_qs.filter(person_type="contractor").order_by("-reportable_comp")
        ctx["schedule_j"] = people_qs.filter(total_comp_sch_j__isnull=False).order_by("-total_comp_sch_j")
    return ctx
```

### Navigation

Add to the sidebar in `base.html`:
- **990 Index** link → `/dashboard/990-index/`
- **990 Parse** link → `/dashboard/990-parse/`

The org detail page is already linked from the org list — no additional nav needed.

### Currency Formatting

All compensation amounts are whole dollars (BIGINT in DB). Display with `$` prefix and comma grouping. Use a custom template filter:

```python
@register.filter
def currency(value):
    if value is None:
        return "—"
    return f"${value:,}"
```

## Acceptance Criteria

### Org Detail Enhancement
- **AC1**: Org detail page shows "Filings" section with all filing_index rows for this EIN
- **AC2**: Filings sorted by tax_period descending
- **AC3**: Filing status displayed with color coding (parsed=green, error=red, downloaded=blue, indexed=gray)
- **AC4**: "Officers & Directors" table shows non-contractor people from latest filing
- **AC5**: Officers table sorted by reportable_comp DESC, then person_name ASC
- **AC6**: Person type color-coded (officer=blue, director=gray, key_employee=green, highest_compensated=amber)
- **AC7**: "Top Contractors" table shows contractor-type people from latest filing
- **AC8**: Contractors sorted by compensation DESC
- **AC9**: "Schedule J Detail" section collapsed by default, expandable
- **AC10**: Schedule J only shows people where total_comp_sch_j IS NOT NULL
- **AC11**: All compensation values formatted as currency ($X,XXX)
- **AC12**: Org detail page renders correctly for orgs with zero filings (graceful empty state)
- **AC13**: Org detail page renders correctly for filings with zero people (parser returned no Part VII)
- **AC14**: Cross-filing comparison shows same person's compensation across multiple filings when toggle enabled

### 990 Index Pipeline Control
- **AC15**: Pipeline control page at `/dashboard/990-index/`
- **AC16**: Form requires either State or EIN (client-side and server-side validation)
- **AC17**: Years field validates as comma-separated 4-digit years
- **AC18**: Form submission creates a Job with phase="990-index"
- **AC19**: Job maps to `enrich_990 --index-only --state XX --years YYYY`
- **AC20**: `--index-only` flag runs only index download, skips parse/import
- **AC21**: Status section shows filing_index row counts by status
- **AC22**: Sidebar includes "990 Index" link

### 990 Parse/Import Pipeline Control
- **AC23**: Pipeline control page at `/dashboard/990-parse/`
- **AC24**: Form requires either State or EIN
- **AC25**: Skip Download and Reparse checkboxes map to `--skip-download` and `--reparse` flags
- **AC26**: Form submission creates a Job with phase="990-parse"
- **AC27**: Job maps to `enrich_990 --parse-only --state XX --years YYYY [--skip-download] [--reparse] [--limit N]`
- **AC28**: `--parse-only` flag skips index download, runs only parse/import
- **AC29**: Status section shows filings by status + people count + error count
- **AC30**: Sidebar includes "990 Parse" link

### Infrastructure
- **AC31**: Two new COMMAND_MAP entries: `990-index` and `990-parse`
- **AC32**: Unmanaged Django models for FilingIndex and Person route to pipeline database
- **AC33**: Currency template filter formats BIGINT as $X,XXX with null handling
- **AC34**: All new views require login (LoginRequiredMixin)
- **AC35**: Audit logging for job creation (same pattern as existing pipeline controls)

## Security Considerations

- **Read-only data access**: People/filing data displayed via unmanaged models with read-only database routing. No writes to pipeline DB from dashboard.
- **Input validation**: EIN validated as exactly 9 digits. Years validated as 4-digit integers. State validated against US_STATES whitelist. All validation server-side (forms.py) in addition to client-side.
- **Authentication**: All views behind LoginRequiredMixin (existing pattern).
- **No PII exposure**: 990 data is public record (IRS publishes it). Names and compensation are already publicly available via TEOS.
- **SQL injection**: All queries via Django ORM (unmanaged models) or parameterized text() queries. No string interpolation.

## Testing Requirements

- **Unit tests**: Form validation (EIN format, years format, state-or-ein required)
- **View tests**: Each view returns 200 with expected context keys, 302 for unauthenticated
- **Template tests**: Currency filter formatting, empty state rendering
- **Integration tests**: Job creation flow for both pipeline phases
- **CLI tests**: `--index-only` and `--parse-only` flags work correctly and are mutually exclusive with each other

## Open Questions

1. Should the two 990 pipeline controls be separate sidebar entries or combined into a single "990 Enrich" page with tabs?
2. Should the org detail page show a filing picker dropdown (to view people from any filing) or always default to most recent?

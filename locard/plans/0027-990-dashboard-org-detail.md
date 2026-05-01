# Plan 0027: 990 Dashboard — Org Detail View & Pipeline Controls

**Spec**: `locard/specs/0027-990-dashboard-org-detail.md`  
**Date**: 2026-05-01

## Overview

Add two pipeline control pages (990 Index, 990 Parse/Import) and enhance the org detail page with 990 leadership/compensation data. 47 acceptance criteria across 3 goals plus infrastructure.

## Implementation Phases

### Phase 1: CLI Flags (enrich_990.py)

**File**: `lavandula/nonprofits/tools/enrich_990.py`

Add `--index-only` and `--parse-only` as a mutually exclusive argparse group:

```python
mode_group = ap.add_mutually_exclusive_group()
mode_group.add_argument("--index-only", action="store_true",
    help="Run only index download, skip parse/import")
mode_group.add_argument("--parse-only", action="store_true",
    help="Skip index download, run only parse/import")
```

In `main()`:
- If `--index-only`: run the `download_and_filter_index()` loop, skip `process_filings()`
- If `--parse-only`: skip the index loop, run only `process_filings()`
- Neither: current behavior (both)

**AC35 (parse-only with zero filings)**: `process_filings()` already handles an empty query result gracefully — it returns stats with all zeros. The CLI will log `"Done: processed=0 ..."` which satisfies "0 filings processed". No code change needed; verify with a test case that mocks an empty filing set.

**ACs**: 25, 34, 35, 47

### Phase 2: COMMAND_MAP & Orchestrator

**File**: `lavandula/dashboard/pipeline/orchestrator.py`

**2a. COMMAND_MAP entries**

Replace the single `990-enrich` entry with two new entries:

```python
"990-index": {
    "cmd": ["python3", "-m", "lavandula.nonprofits.tools.enrich_990", "--index-only"],
    "params": {
        "state": {"type": "choice", "choices": US_STATES, "flag": "--state"},
        "ein": {"type": "text", "pattern": r"^\d{9}$", "flag": "--ein"},
        "years": {"type": "text", "pattern": r"^\d{4}(\s*,\s*\d{4})*$", "flag": "--years"},
    },
},
"990-parse": {
    "cmd": ["python3", "-m", "lavandula.nonprofits.tools.enrich_990", "--parse-only"],
    "params": {
        "state": {"type": "choice", "choices": US_STATES, "flag": "--state"},
        "ein": {"type": "text", "pattern": r"^\d{9}$", "flag": "--ein"},
        "years": {"type": "text", "pattern": r"^\d{4}(\s*,\s*\d{4})*$", "flag": "--years"},
        "limit": {"type": "int", "min": 1, "max": 999999, "flag": "--limit"},
        "skip_download": {"type": "bool", "flag": "--skip-download"},
        "reparse": {"type": "bool", "flag": "--reparse"},
    },
},
```

Note: bake `--index-only` / `--parse-only` into the `cmd` base list, not as a param. The flag is not user-selectable — it's intrinsic to the phase.

Keep the existing `990-enrich` entry so any in-flight or historical jobs don't break. Mark it with a comment as legacy.

**2b. Job creation functions**

Add two new functions following the `create_classify_job` pattern:

```python
def create_990_index_job(config_overrides: dict, host: str) -> Job:
    """Create a 990-index job. Blocks duplicates globally."""
    # transaction.atomic + select_for_update, check for pending/running 990-index
    # state_code from config_overrides.get("state") or None
    ...

def create_990_parse_job(config_overrides: dict, host: str) -> Job:
    """Create a 990-parse job. Blocks duplicates globally."""
    # Same pattern, phase="990-parse"
    ...
```

**2c. Job model PHASE_CHOICES**

**File**: `lavandula/dashboard/pipeline/models.py`

Add to `PHASE_CHOICES`:
```python
("990-index", "990 Index"),
("990-parse", "990 Parse"),
```

**ACs**: 23, 24, 32, 33, 40, 28, 39

### Phase 3: Unmanaged Models

**File**: `lavandula/dashboard/pipeline/models.py`

Add two unmanaged models after the existing `CrawledOrg` model:

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

**Schema verification (AC41)**: The unmanaged models MUST match migration 010 exactly. The migration has columns not yet reflected above that must be added:

- `FilingIndex`: add `sub_date = models.CharField(max_length=20, null=True)`, `return_ts = models.DateTimeField(null=True)`, `is_amended = models.BooleanField(default=False)`, `parsed_at = models.DateTimeField(null=True)`
- `Person`: fix `avg_hours_per_week` to `DecimalField(max_digits=5, decimal_places=1)` (migration uses `NUMERIC(5,1)`, not `(5,2)`)

The builder must diff the model field list against migration 010 before writing any views.

**Router verification**: The existing `PipelineRouter` in `routers.py` uses `model._meta.managed == False` — no changes needed. Both new models have `managed = False` and will route to the `pipeline` DB alias automatically. Merge this verification into Phase 3 (not a separate phase).

**ACs**: 41, 42

### Phase 4: Forms

**File**: `lavandula/dashboard/pipeline/forms.py`

Two new form classes:

```python
class EnrichIndexForm(forms.Form):
    state = forms.ChoiceField(
        choices=[("", "—")] + [(s, s) for s in US_STATES],
        required=False,
        widget=forms.Select(attrs={"class": _SELECT}),
    )
    ein = forms.CharField(
        max_length=9, required=False,
        widget=forms.TextInput(attrs={"class": _SELECT, "placeholder": "123456789"}),
    )
    years = forms.CharField(
        initial=str(datetime.date.today().year),
        widget=forms.TextInput(attrs={"class": _SELECT, "placeholder": "2023,2024"}),
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("state") and not cleaned.get("ein"):
            raise forms.ValidationError("State or EIN is required")
        if cleaned.get("ein") and not re.match(r"^\d{9}$", cleaned["ein"]):
            raise forms.ValidationError("EIN must be exactly 9 digits")
        years_str = cleaned.get("years", "")
        if not re.match(r"^\d{4}(\s*,\s*\d{4})*$", years_str):
            raise forms.ValidationError("Years must be comma-separated 4-digit years")
        year_list = [int(y.strip()) for y in years_str.split(",")]
        current_year = datetime.date.today().year
        for y in year_list:
            if y < 2019 or y > current_year:
                raise forms.ValidationError(f"Year {y} outside valid range [2019, {current_year}]")
        if len(year_list) > 5:
            raise forms.ValidationError("Maximum 5 years per request")
        return cleaned


class EnrichParseForm(forms.Form):
    # Same state/ein/years fields as EnrichIndexForm
    state = ...  # identical
    ein = ...    # identical
    years = ...  # identical
    limit = forms.IntegerField(
        required=False, min_value=1, max_value=999999,
        widget=forms.NumberInput(attrs={"class": _SELECT, "placeholder": "Optional"}),
    )
    skip_download = forms.BooleanField(required=False, label="Skip Download (cached only)")
    reparse = forms.BooleanField(required=False, label="Reparse errors")

    def clean(self):
        # Same state-or-ein + years validation as EnrichIndexForm
        ...
```

Extract shared validation into a helper or mixin to avoid duplication of the state-or-ein + years + max-5-years logic.

**ACs**: 21, 22, 30, 31

### Phase 5: Views

**File**: `lavandula/dashboard/pipeline/views.py`

**5a. Pipeline control views** (follow ClassifierView/ClassifyJobCreateView pattern)

```python
class EnrichIndexView(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/990_index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["running_job"] = Job.objects.filter(phase="990-index", status="running").first()
        ctx["pending_job"] = Job.objects.filter(phase="990-index", status="pending").first()
        from .forms import EnrichIndexForm
        ctx["form"] = EnrichIndexForm()

        # Status: global or scoped by state/ein + years
        state = self.request.GET.get("state")
        ein = self.request.GET.get("ein")
        years_param = self.request.GET.get("years")
        qs = FilingIndex.objects.using("pipeline").all()
        scoped = bool(state or ein or years_param)
        if scoped:
            if ein:
                qs = qs.filter(ein=ein)
            elif state:
                qs = qs.filter(ein__in=NonprofitSeed.objects.filter(state=state).values_list("ein", flat=True))
            if years_param:
                year_list = [int(y) for y in years_param.split(",") if y.strip().isdigit()]
                qs = qs.filter(filing_year__in=year_list)
        ctx["status_counts"] = qs.values("status").annotate(count=models.Count("status"))
        ctx["total_filings"] = qs.count()
        ctx["scoped"] = scoped
        return ctx


class EnrichIndexJobCreateView(LoginRequiredMixin, View):
    def post(self, request):
        from .forms import EnrichIndexForm
        form = EnrichIndexForm(request.POST)
        if not form.is_valid():
            messages.error(request, f"Invalid form: {form.errors.as_text()}")
            return redirect("enrich_index")

        config = {k: v for k, v in form.cleaned_data.items() if v not in (None, "", False)}
        try:
            job = create_990_index_job(config, _get_hostname())
            _log_audit(request, "job_create", "990-index", {"job_id": job.pk})
            messages.success(request, f"Created 990 index job #{job.pk}")
        except DuplicateJobError as e:
            messages.error(request, str(e))

        # Redirect with scope params (state or ein + years)
        params = {}
        if config.get("ein"):
            params["ein"] = config["ein"]
        elif config.get("state"):
            params["state"] = config["state"]
        if config.get("years"):
            params["years"] = config["years"]
        if params:
            from urllib.parse import urlencode
            return redirect(f"{reverse('enrich_index')}?{urlencode(params)}")
        return redirect("enrich_index")
```

Same pattern for `EnrichParseView` / `EnrichParseJobCreateView`, with these additions:

```python
# In EnrichParseView.get_context_data():

# Cache status (AC37)
cache_dir = Path.home() / ".lavandula" / "990-cache"
try:
    zips = list(cache_dir.glob("*.zip"))
    ctx["cache_count"] = len(zips)
    ctx["cache_size_gb"] = sum(f.stat().st_size for f in zips) / (1024 ** 3)
except (OSError, PermissionError):
    ctx["cache_count"] = None  # signals "Cache: unavailable" in template

# Scoped status + people count (AC36)
# Same state/ein/years scoping as EnrichIndexView for filing counts.
# People count: join Person table filtered by the same EIN set and years.
state = self.request.GET.get("state")
ein = self.request.GET.get("ein")
years_param = self.request.GET.get("years")
people_qs = Person.objects.using("pipeline").all()
if ein:
    people_qs = people_qs.filter(ein=ein)
elif state:
    people_qs = people_qs.filter(ein__in=NonprofitSeed.objects.filter(state=state).values_list("ein", flat=True))
# Years filter on people uses the tax_period prefix (YYYY from YYYYMM)
if years_param:
    year_list = [str(y) for y in years_param.split(",") if y.strip().isdigit()]
    people_qs = people_qs.filter(tax_period__regex=r"^(" + "|".join(year_list) + r")")
ctx["people_count"] = people_qs.count()
```

**5b. OrgDetailView enhancement**

Extend existing `OrgDetailView` (currently just a bare `DetailView`) to add 990 context:

```python
class OrgDetailView(LoginRequiredMixin, DetailView):
    model = NonprofitSeed
    template_name = "pipeline/org_detail.html"
    context_object_name = "org"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ein = self.object.ein

        filings = FilingIndex.objects.using("pipeline").filter(
            ein=ein
        ).order_by("-tax_period", "-object_id")
        ctx["filings"] = filings

        # Filing picker
        selected_oid = self.request.GET.get("filing")
        selected = None
        if selected_oid:
            selected = filings.filter(object_id=selected_oid).first()
        if selected is None:
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

        # Cross-filing comparison data
        if filings.count() > 1:
            all_people = Person.objects.using("pipeline").filter(ein=ein)
            # Build comparison matrix: {person_name: {object_id: reportable_comp}}
            comparison = {}
            for p in all_people:
                comparison.setdefault(p.person_name, {})[p.object_id] = p.reportable_comp
            ctx["comparison"] = comparison
            # Build filing headers with amended badge
            tp_counts = {}
            for f in filings:
                tp_counts[f.tax_period] = tp_counts.get(f.tax_period, 0) + 1
            ctx["filing_headers"] = [
                {
                    "object_id": f.object_id,
                    "label": f.tax_period + (" (amended)" if tp_counts[f.tax_period] > 1 else ""),
                }
                for f in filings
            ]
        return ctx
```

**ACs**: 1-19, 20, 26, 29, 36, 37, 44, 46

### Phase 6: Templates

**6a. Currency template filter**

**File**: `lavandula/dashboard/pipeline/templatetags/pipeline_tags.py`

```python
@register.filter
def currency(value):
    if value is None:
        return "—"
    return f"${value:,}"
```

**AC**: 43

**6b. `990_index.html`**

**File**: `lavandula/dashboard/pipeline/templates/pipeline/990_index.html`

Follow the classifier.html layout:
- Left column: Status panel (global/scoped filing counts by status) + Job queue form
- Right columns: Filing counts table

Status panel shows counts per status (indexed, downloaded, parsed, error) with color coding matching the filing status semantics from spec.

**ACs**: 20, 26

**6c. `990_parse.html`**

**File**: `lavandula/dashboard/pipeline/templates/pipeline/990_parse.html`

Same layout as 990_index.html plus:
- Cache status line: "Cache: X zips, Y.Z GB" or "Cache: unavailable"
- Total people count in status panel

**ACs**: 29, 36, 37

**6d. `org_detail.html` enhancement**

**File**: `lavandula/dashboard/pipeline/templates/pipeline/org_detail.html`

Extend the existing template. Below the current org info section, add:

1. **Filing summary table** — all filings with status color badges
2. **Filing picker dropdown** — `<select>` that triggers `window.location` change on selection
3. **Officers & Directors table** — with person_type color badges (officer=blue, director=gray, key_employee=green, highest_compensated=amber)
4. **Top Contractors table** — name, services_desc, compensation
5. **Schedule J detail** — `<details>` element (collapsed by default) with full compensation breakdown
6. **Cross-filing comparison** — `<details>` toggle, matrix table with person names × filing columns
7. **Empty states** — "No 990 filings found" / "No leadership data extracted"

Person type color mapping (Tailwind classes):
```
officer       → bg-blue-100 text-blue-800
director      → bg-gray-100 text-gray-800
key_employee  → bg-green-100 text-green-800
highest_compensated → bg-amber-100 text-amber-800
```

Filing status color mapping:
```
parsed     → bg-green-100 text-green-800
error      → bg-red-100 text-red-800
downloaded → bg-blue-100 text-blue-800
indexed    → bg-gray-100 text-gray-800
```

**ACs**: 1-19

### Phase 7: URLs & Navigation

**7a. URL patterns**

**File**: `lavandula/dashboard/pipeline/urls.py`

Add after the classifier routes:

```python
# 990 Pipeline Controls
path("990-index/", views.EnrichIndexView.as_view(), name="enrich_index"),
path("990-index/queue/", views.EnrichIndexJobCreateView.as_view(), name="enrich_index_job_create"),
path("990-parse/", views.EnrichParseView.as_view(), name="enrich_parse"),
path("990-parse/queue/", views.EnrichParseJobCreateView.as_view(), name="enrich_parse_job_create"),
```

**7b. Sidebar navigation**

**File**: `lavandula/dashboard/pipeline/templates/pipeline/base.html`

Add to the PIPELINE section after Classifier:
```html
<a href="{% url 'enrich_index' %}" class="...">990 Index</a>
<a href="{% url 'enrich_parse' %}" class="...">990 Parse</a>
```

Active link detection: match `'enrich_index'` and `'enrich_parse'` in `url_name`.

**ACs**: 27, 38

### Phase 8: Tests

**Unit tests** (`lavandula/dashboard/pipeline/tests/`):

1. **Form tests** (`test_forms.py`):
   - `EnrichIndexForm`: valid state-only, valid ein-only, both blank → error, invalid EIN format, invalid years format, >5 years → error, year out of range
   - `EnrichParseForm`: same as above + limit bounds, checkbox defaults

2. **View tests** (`test_views.py`):
   - Each new view returns 200 for authenticated user
   - Each new view returns 302 for unauthenticated (AC44)
   - Job create views accept only POST, reject GET with 405 (AC45)
   - Job create views include CSRF token validation (AC45)
   - OrgDetailView returns filing context for orgs with filings
   - OrgDetailView handles zero filings gracefully — response contains "No 990 filings found" (AC14)
   - OrgDetailView handles filings with zero people — response contains "No leadership data extracted" (AC15)
   - OrgDetailView handles invalid `?filing=` param — `selected_filing` falls back to first (AC19)
   - OrgDetailView with multiple filings includes `comparison` and `filing_headers` in context
   - OrgDetailView with single filing does NOT include comparison context
   - Filing picker default: `selected_filing` matches first in sort order when no `?filing=` param (AC4)
   - Job create views return redirect on valid POST
   - Job create views return error on duplicate (AC28, AC39)
   - Status panel scoping: GET with `?state=NY&years=2024` returns scoped counts; GET without params returns global counts (AC26, AC36)

3. **Template filter tests** (`test_template_tags.py`):
   - `currency(123456)` → `"$123,456"`
   - `currency(0)` → `"$0"`
   - `currency(None)` → `"—"`

4. **CLI tests** (`lavandula/nonprofits/tests/unit/test_enrich_990.py`):
   - `--index-only` runs index loop, skips process_filings
   - `--parse-only` skips index loop, runs process_filings
   - `--parse-only` with zero indexed filings: completes successfully, logs "processed=0" (AC35)
   - Both flags simultaneously → argparse error (AC47)
   - Neither flag → both run (existing behavior)

5. **Orchestrator tests** (`test_orchestrator.py`):
   - `build_argv("990-index", {"state": "NY", "years": "2024"})` produces correct argv
   - `build_argv("990-parse", {"state": "NY", "skip_download": True})` produces correct argv
   - `create_990_index_job` blocks duplicates
   - `create_990_parse_job` blocks duplicates

**ACs**: all testing requirements from spec

## File Manifest

| File | Action | Phase |
|------|--------|-------|
| `lavandula/nonprofits/tools/enrich_990.py` | Edit | 1 |
| `lavandula/dashboard/pipeline/orchestrator.py` | Edit | 2 |
| `lavandula/dashboard/pipeline/models.py` | Edit | 2, 3 |
| `lavandula/dashboard/pipeline/forms.py` | Edit | 4 |
| `lavandula/dashboard/pipeline/views.py` | Edit | 5 |
| `lavandula/dashboard/pipeline/templatetags/pipeline_tags.py` | Edit | 6 |
| `lavandula/dashboard/pipeline/templates/pipeline/990_index.html` | Create | 6 |
| `lavandula/dashboard/pipeline/templates/pipeline/990_parse.html` | Create | 6 |
| `lavandula/dashboard/pipeline/templates/pipeline/org_detail.html` | Edit | 6 |
| `lavandula/dashboard/pipeline/urls.py` | Edit | 7 |
| `lavandula/dashboard/pipeline/templates/pipeline/base.html` | Edit | 7 |
| `lavandula/dashboard/pipeline/tests/test_forms.py` | Edit | 8 |
| `lavandula/dashboard/pipeline/tests/test_views.py` | Edit | 8 |
| `lavandula/dashboard/pipeline/tests/test_template_tags.py` | Edit | 8 |
| `lavandula/nonprofits/tests/unit/test_enrich_990.py` | Create | 8 |
| `lavandula/dashboard/pipeline/tests/test_orchestrator.py` | Edit | 8 |

## Key Decisions

1. **Bake `--index-only`/`--parse-only` into COMMAND_MAP `cmd`**, not as user-selectable params. The mode is determined by which page the user submits from, not a form checkbox.

2. **Keep legacy `990-enrich` COMMAND_MAP entry** so historical/in-flight jobs still render correctly in the Jobs list. No new jobs will use it. A running legacy `990-enrich` job does NOT block new `990-index` or `990-parse` jobs — they are different phase strings and the duplicate check is per-phase. This is acceptable because the operator won't accidentally launch `990-enrich` from the dashboard (no UI for it), and if they run it from CLI while a dashboard job is active, the underlying pipeline has its own file-level locking.

3. **Extract shared form validation** (state-or-ein, years format, year bounds) into a mixin or helper to avoid copy-paste between `EnrichIndexForm` and `EnrichParseForm`.

4. **Cross-filing comparison query**: fetch all people for the EIN in one query rather than N queries per filing. Build the comparison matrix in Python. Acceptable because orgs typically have <100 people across all filings.

5. **Cache status scan**: synchronous `os.listdir` + `stat` — not recursive, only counts `*.zip` in the top-level cache dir. Lightweight enough to run on every page load.

6. **`person_type` has granular values**: The `_derive_person_type()` function in `irs990_parser.py` already writes specific values: `officer`, `director`, `key_employee`, `highest_compensated`, `contractor`, `listed`. The template can use `person_type` directly for badge color mapping (AC8) — no need to read boolean flags.

## Traps to Avoid

1. **Don't forget `.using("pipeline")`** on every FilingIndex/Person query. The default DB alias won't have these tables.

2. **Cross-filing comparison "amended" badge**: count tax_periods across filings first, then apply badge to any filing whose tax_period appears more than once. Don't compare adjacent filings — multiple amendments for the same period are possible.

3. **Filing picker fallback**: if `?filing=X` references a valid object_id but for a different EIN, it must still fall back. Filter by both `object_id=X` AND the current EIN's filings queryset.

4. **Job phase strings must match exactly**: `"990-index"` and `"990-parse"` (with hyphens). The Job model allows max_length=20, which is sufficient.

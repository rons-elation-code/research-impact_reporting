# Plan 0032: Dashboard & Phase Pages for National Ingest Tracking

**Spec**: `locard/specs/0032-dashboard-national-ingest.md`

## Overview

Replace the aggregate dashboard with a state × pipeline-stage progress grid. Add recent jobs tables and stats-by-state pills to all phase pages, matching the seeder's pattern. Add config display on running jobs.

The implementation touches 2 Python files and 8 templates. No migrations, no new models, no new URLs.

## Implementation Order

Work in this order to get incremental value and testability:

1. **Config display helper** (views.py) — shared utility used by all pages
2. **Main dashboard rewrite** (views.py + dashboard_stats.html) — highest value, most complex
3. **Resolver page** (views.py + resolver.html) — establishes the phase page pattern
4. **Classifier, crawler, phone enrich pages** — apply the same pattern
5. **990 index, 990 parse pages** — simpler variant (jobs only, no state stats)

## Step 1: Job Config Display Helper

**File**: `views.py`

Add a helper function near the top (after imports, before view classes) that extracts display-worthy config from `job.config_json`:

```python
_CONFIG_ALLOWLIST = {
    "seed": ["states", "target", "ntee_majors"],
    "resolve": ["state", "search_engines", "llm_model", "brave_qps", "search_qps", "consumer_threads", "limit"],
    "crawl": ["state", "limit"],
    "classify": ["state", "llm_model", "definition", "limit", "re_classify"],
    "enrich-phone": ["state", "search_engines", "limit"],
    "990-index": ["filing_year"],
    "990-parse": ["filing_year", "limit"],
}

def _format_job_config(job):
    """Extract allowlisted config keys for display. Returns list of 'key=value' strings."""
    if not job or not job.config_json:
        return []
    allowed = _CONFIG_ALLOWLIST.get(job.phase, [])
    parts = []
    for key in allowed:
        val = job.config_json.get(key)
        if val is not None and val != "":
            parts.append(f"{key}={val}")
    return parts
```

Also add a helper to annotate jobs with config display and elapsed time:

```python
from django.utils import timezone

def _annotate_running_jobs(jobs):
    """Add config_display and elapsed to running job queryset results."""
    now = timezone.now()
    annotated = []
    for job in jobs:
        job.config_display = _format_job_config(job)
        if job.started_at:
            delta = now - job.started_at
            mins = int(delta.total_seconds() // 60)
            job.elapsed = f"{mins}m ago" if mins > 0 else "just started"
        else:
            job.elapsed = "pending"
        annotated.append(job)
    return annotated
```

## Step 2: Main Dashboard Rewrite

### 2a. Rewrite `_dashboard_stats()` in views.py (lines 96-146)

Replace the entire function. The new version runs two raw SQL queries against `connections["pipeline"]` and one Django ORM query for jobs:

```python
from django.db import connections

def _dashboard_stats():
    # --- State progress from lava_corpus ---
    with connections["pipeline"].cursor() as cursor:
        cursor.execute("""
            SELECT
                s.state,
                COUNT(*) as seeded,
                SUM(CASE WHEN s.resolver_status = 'resolved' THEN 1 ELSE 0 END) as resolved,
                COUNT(DISTINCT co.ein) as crawled,
                SUM(CASE WHEN s.phone IS NOT NULL AND s.phone != '' THEN 1 ELSE 0 END) as has_phone
            FROM nonprofits_seed s
            LEFT JOIN crawled_orgs co ON s.ein = co.ein
            GROUP BY s.state
            ORDER BY COUNT(*) DESC
        """)
        columns = [col[0] for col in cursor.description]
        state_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT
                s.state,
                COUNT(DISTINCT c.content_sha256) as total_reports,
                COUNT(DISTINCT CASE WHEN c.classification IS NOT NULL THEN c.content_sha256 END) as classified
            FROM nonprofits_seed s
            JOIN corpus c ON s.ein = c.source_org_ein
            GROUP BY s.state
        """)
        columns = [col[0] for col in cursor.description]
        report_rows = {row[0]: dict(zip(columns, row)) for row in cursor.fetchall()}

    # Merge report data into state rows
    for row in state_rows:
        rpt = report_rows.get(row["state"], {})
        row["total_reports"] = rpt.get("total_reports", 0)
        row["classified"] = rpt.get("classified", 0)
        # Compute resolved percentage
        row["resolved_pct"] = round(row["resolved"] / row["seeded"] * 100) if row["seeded"] > 0 else 0

    # --- Running and recent jobs from lava_dashboard ---
    running_jobs = _annotate_running_jobs(
        Job.objects.filter(status="running").select_related()
    )

    # Determine which states have active jobs (for row ordering)
    active_states = set()
    for job in Job.objects.filter(status__in=["running", "pending"]):
        if job.state_code:
            active_states.add(job.state_code)

    # Sort: active states first, then by seeded desc
    state_rows.sort(key=lambda r: (0 if r["state"] in active_states else 1, -r["seeded"]))
    for row in state_rows:
        row["is_active"] = row["state"] in active_states

    recent_jobs = Job.objects.order_by("-created_at")[:10]

    return {
        "state_rows": state_rows,
        "running_jobs": running_jobs,
        "recent_jobs": recent_jobs,
        "active_states": active_states,
    }
```

**Note**: On the `pipeline` connection, `search_path=lava_corpus,public`, so we use unqualified table names (`nonprofits_seed`, `corpus`, `crawled_orgs`).

### 2b. Rewrite `dashboard_stats.html`

Replace the entire template content. New structure:

```
<!-- Running Jobs (if any) -->
{% if running_jobs %}
<div class="mb-6">
  <h2 class="text-lg font-semibold mb-3">Running Jobs</h2>
  {% for job in running_jobs %}
  <div class="flex items-center gap-3 p-3 bg-blue-50 border border-blue-200 rounded mb-2">
    <span class="h-3 w-3 rounded-full bg-blue-500 animate-pulse"></span>
    <span class="font-mono text-sm">
      Job #{{ job.pk }} — {{ job.phase }} {{ job.state_code|default:"—" }}
      {% for param in job.config_display %} | {{ param }}{% endfor %}
      | {{ job.elapsed }}
    </span>
  </div>
  {% endfor %}
</div>
{% endif %}

<!-- State Progress Table -->
<h2 class="text-lg font-semibold mb-3">National Ingest Progress</h2>
<div class="overflow-x-auto">
  <table class="min-w-full text-sm">
    <thead class="bg-gray-100">
      <tr>
        <th class="px-3 py-2 text-left">State</th>
        <th class="px-3 py-2 text-right">Seeded</th>
        <th class="px-3 py-2 text-right">Resolved</th>
        <th class="px-3 py-2 text-right">Crawled</th>
        <th class="px-3 py-2 text-right">Classified</th>
        <th class="px-3 py-2 text-right">Reports</th>
      </tr>
    </thead>
    <tbody>
      {% for row in state_rows %}
      <tr class="border-b {% if row.is_active %}border-l-4 border-l-blue-500 bg-blue-50{% endif %}">
        <td class="px-3 py-2 font-medium">{{ row.state }}</td>
        <td class="px-3 py-2 text-right">{{ row.seeded|intcomma }}</td>
        <td class="px-3 py-2 text-right">
          <span class="{% if row.resolved_pct == 0 %}text-gray-400{% elif row.resolved_pct < 80 %}text-yellow-600{% else %}text-green-600{% endif %}">
            {{ row.resolved|intcomma }} / {{ row.seeded|intcomma }} ({{ row.resolved_pct }}%)
          </span>
        </td>
        <td class="px-3 py-2 text-right">{{ row.crawled|intcomma }}</td>
        <td class="px-3 py-2 text-right">{{ row.classified|intcomma }}</td>
        <td class="px-3 py-2 text-right">{{ row.total_reports|intcomma }}</td>
      </tr>
      {% empty %}
      <tr><td colspan="6" class="px-3 py-4 text-center text-gray-400">No states seeded yet</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<!-- Recent Jobs Table -->
<h2 class="text-lg font-semibold mt-6 mb-3">Recent Jobs</h2>
<table class="min-w-full text-sm">
  ... standard job table (ID, Phase, State, Status, Exit, Duration, Created) ...
</table>
```

**Template filter**: Uses `|intcomma` from `django.contrib.humanize`. Verify `django.contrib.humanize` is in INSTALLED_APPS and `{% load humanize %}` at top of template. If not available, format numbers in the view.

### 2c. Verify `dashboard.html` wrapper

The main `dashboard.html` should remain unchanged — it already wraps the partial with HTMX polling:
```html
<div hx-get="{% url 'dashboard_stats' %}" hx-trigger="every 5s" hx-swap="innerHTML">
  {% include "pipeline/partials/dashboard_stats.html" %}
</div>
```

No changes needed here.

## Step 3: Resolver Page

### 3a. Update `ResolverView.get_context_data()` (views.py ~line 369)

Add `recent_jobs` and `resolve_stats` to context. Keep existing `recent_results`, `running_job`, `pending_job`, and `form`.

```python
def get_context_data(self, **kwargs):
    ctx = super().get_context_data(**kwargs)
    ctx["running_job"] = Job.objects.filter(phase="resolve", status="running").first()
    ctx["pending_job"] = Job.objects.filter(phase="resolve", status="pending").first()
    if ctx["running_job"]:
        ctx["running_job"] = _annotate_running_jobs([ctx["running_job"]])[0]
    ctx["recent_results"] = NonprofitSeed.objects.filter(
        resolver_updated_at__isnull=False
    ).order_by("-resolver_updated_at")[:50]
    ctx["recent_jobs"] = Job.objects.filter(phase="resolve").order_by("-created_at")[:20]
    ctx["resolve_stats"] = (
        NonprofitSeed.objects.values("state")
        .annotate(
            total=Count("ein"),
            resolved=Count("ein", filter=models.Q(resolver_status="resolved")),
        )
        .order_by("state")
    )
    from .forms import ResolverForm
    ctx["form"] = ResolverForm()
    return ctx
```

### 3b. Update `resolver.html`

Keep the existing left column (status + form). Replace the right column content. Add between the existing Recent Results table and the form:

**New sections to add in the right column (2/3 width):**

1. **Running job config** — enhance the existing status box to show config params
2. **Resolve Stats by State** — pill grid matching seeder pattern
3. **Recent Resolve Jobs** — job table matching seeder pattern
4. **Recent Results** — keep existing table below

Stats by state pill grid template pattern:
```html
<h3 class="text-md font-semibold mb-2">Resolve Stats by State</h3>
<div class="flex flex-wrap gap-2 mb-4">
  {% for stat in resolve_stats %}
  {% if stat.total > 0 %}
  <span class="px-2 py-1 rounded text-xs font-mono
    {% if stat.resolved == 0 %}bg-gray-100 text-gray-500
    {% elif stat.resolved < stat.total|multiply:0.8 %}bg-yellow-100 text-yellow-700
    {% else %}bg-green-100 text-green-700{% endif %}">
    {{ stat.state }}: {{ stat.resolved }}/{{ stat.total }}
    ({% widthratio stat.resolved stat.total 100 %}%)
  </span>
  {% endif %}
  {% endfor %}
</div>
```

**Note**: `{% widthratio %}` computes integer percentages without a custom filter. Alternatively, compute the percentage in the view and pass it as a field.

Recent jobs table — reuse the same table pattern from the seeder:
```html
<h3 class="text-md font-semibold mb-2">Recent Resolve Jobs</h3>
<table class="min-w-full text-sm">
  <thead class="bg-gray-100">
    <tr>
      <th class="px-2 py-1 text-left">ID</th>
      <th class="px-2 py-1 text-left">State</th>
      <th class="px-2 py-1 text-left">Status</th>
      <th class="px-2 py-1 text-left">Exit</th>
      <th class="px-2 py-1 text-left">Progress</th>
      <th class="px-2 py-1 text-left">Duration</th>
      <th class="px-2 py-1 text-left">Created</th>
    </tr>
  </thead>
  <tbody>
    {% for job in recent_jobs %}
    <tr class="border-b">
      <td class="px-2 py-1"><a href="{% url 'job_detail' job.pk %}" class="text-blue-600">{{ job.pk }}</a></td>
      <td class="px-2 py-1">{{ job.state_code|default:"—" }}</td>
      <td class="px-2 py-1">{{ job.status }}</td>
      <td class="px-2 py-1">{{ job.exit_code|default:"—" }}</td>
      <td class="px-2 py-1">{% if job.progress_total %}{{ job.progress_current }}/{{ job.progress_total }}{% else %}—{% endif %}</td>
      <td class="px-2 py-1">{% if job.finished_at and job.started_at %}...{% else %}—{% endif %}</td>
      <td class="px-2 py-1">{{ job.created_at|timesince }} ago</td>
    </tr>
    {% empty %}
    <tr><td colspan="7" class="px-2 py-4 text-center text-gray-400">No resolve jobs yet</td></tr>
    {% endfor %}
  </tbody>
</table>
```

**Duration calculation**: Use `|timesince` for "Created" column. For Duration, compute `finished_at - started_at` in the view or use a template filter. The seeder already has this pattern — copy it exactly.

## Step 4: Classifier, Crawler, Phone Enrich Pages

Apply the same pattern from Step 3, with per-page variations:

### 4a. Classifier

**View changes** (ClassifierView ~line 402):
- Add `ctx["recent_jobs"] = Job.objects.filter(phase="classify").order_by("-created_at")[:20]`
- Add `ctx["classify_stats"]` — raw SQL against `connections["pipeline"]`:
  ```sql
  SELECT s.state,
         COUNT(DISTINCT c.content_sha256) as total_reports,
         COUNT(DISTINCT CASE WHEN c.classification IS NOT NULL THEN c.content_sha256 END) as classified
  FROM nonprofits_seed s
  JOIN corpus c ON s.ein = c.source_org_ein
  GROUP BY s.state ORDER BY s.state
  ```
- Annotate running_job with config display

**Template changes** (classifier.html):
- Add classify stats pills: `{{ stat.state }}: {{ stat.classified }}/{{ stat.total_reports }}`
- Add recent classify jobs table
- Keep existing Recent Classifications table and ad-hoc process controls

### 4b. Crawler

**View changes** (CrawlerView ~line 384):
- Add `ctx["recent_jobs"] = Job.objects.filter(phase="crawl").order_by("-created_at")[:20]`
- Add `ctx["crawl_stats"]` — raw SQL:
  ```sql
  SELECT s.state,
         SUM(CASE WHEN s.resolver_status = 'resolved' THEN 1 ELSE 0 END) as resolved,
         COUNT(DISTINCT co.ein) as crawled
  FROM nonprofits_seed s
  LEFT JOIN crawled_orgs co ON s.ein = co.ein
  WHERE s.resolver_status = 'resolved'
  GROUP BY s.state ORDER BY s.state
  ```
- Annotate running_job with config display

**Template changes** (crawler.html):
- Add crawl stats pills: `{{ stat.state }}: {{ stat.crawled }}/{{ stat.resolved }}`
- Add recent crawl jobs table
- Keep existing Recently Crawled Orgs and Recent Reports tables

### 4c. Phone Enrich

**View changes** (PhoneEnrichView ~line 690):
- Add `ctx["recent_jobs"] = Job.objects.filter(phase="enrich-phone").order_by("-created_at")[:20]`
- Add `ctx["phone_stats"]` — ORM query:
  ```python
  NonprofitSeed.objects.filter(resolver_status="resolved").values("state").annotate(
      resolved=Count("ein"),
      has_phone=Count("ein", filter=models.Q(phone__isnull=False) & ~models.Q(phone="")),
  ).order_by("state")
  ```
- Annotate running_job with config display

**Template changes** (phone_enrich.html):
- Replace the About section with:
  - Phone stats pills: `{{ stat.state }}: {{ stat.has_phone }}/{{ stat.resolved }}`
  - Recent phone enrich jobs table
- Keep existing status card with phone/resolved counts

## Step 5: 990 Index and 990 Parse Pages

Simpler variant — add recent jobs table only (no state stats).

### 5a. 990 Index

**View changes** (EnrichIndexView ~line 573):
- Add `ctx["recent_jobs"] = Job.objects.filter(phase="990-index").order_by("-created_at")[:20]`
- Annotate running_job with config display

**Template changes** (990_index.html):
- Add recent jobs table after the existing Index Refresh History table
- Enhance running job display to show config params

### 5b. 990 Parse

**View changes** (EnrichParseView ~line 635):
- Add `ctx["recent_jobs"] = Job.objects.filter(phase="990-parse").order_by("-created_at")[:20]`
- Annotate running_job with config display

**Template changes** (990_parse.html):
- Add recent jobs table after the existing Filing & People Counts section
- Enhance running job display to show config params

## Step 6: Template Includes (DRY)

The recent jobs table and stats pill grid appear on 7+ pages. Extract shared partials:

- `templates/pipeline/partials/_recent_jobs_table.html` — takes `recent_jobs` context var, renders the standard table
- `templates/pipeline/partials/_stats_pills.html` — takes `stats` and `numerator_key`/`denominator_key` vars, renders the pill grid with color coding

Each page then uses `{% include %}` with the appropriate context. This prevents divergence across pages.

## Step 7: Verification

After implementation, verify:

1. Dashboard loads with state progress table, running jobs, recent jobs
2. Each phase page shows stats + recent jobs alongside existing content
3. HTMX 5s refresh works on dashboard (check Network tab)
4. Running job config params display correctly for each phase
5. Empty state (no jobs, no data) doesn't break any page
6. Numbers cross-check: dashboard Resolved column matches resolver page stats

## Files Changed Summary

| File | Lines Changed (est.) | Description |
|------|---------------------|-------------|
| `views.py` | ~80 new, ~50 replaced | Config helper, rewrite _dashboard_stats, add context to 6 views |
| `partials/dashboard_stats.html` | ~80 rewrite | State progress table + running/recent jobs |
| `partials/_recent_jobs_table.html` | ~30 new | Shared recent jobs table partial |
| `partials/_stats_pills.html` | ~15 new | Shared stats pill grid partial |
| `resolver.html` | ~40 added | Stats pills + recent jobs sections |
| `classifier.html` | ~40 added | Stats pills + recent jobs sections |
| `crawler.html` | ~40 added | Stats pills + recent jobs sections |
| `phone_enrich.html` | ~40 added | Stats pills + recent jobs, replaces About |
| `990_index.html` | ~30 added | Recent jobs section |
| `990_parse.html` | ~30 added | Recent jobs section |

**Total**: ~10 files, ~400 lines of changes.

## Acceptance Criteria Mapping

| AC# | Verified By |
|-----|------------|
| 1-7 | Dashboard manual test |
| 8-11 | Resolver page manual test |
| 12-15 | Classifier page manual test |
| 16-17 | Phone enrich page manual test |
| 18-20 | Crawler page manual test |
| 21-22 | 990 index page manual test |
| 23-24 | 990 parse page manual test |
| 25 | No migration files created |
| 26 | Page load timing in browser DevTools |
| 27 | Resize browser, check overflow-x-auto and flex-wrap |
| 28 | Spot-check running job display for each phase |

## Risks

1. **Query performance** — the two dashboard aggregation queries hit the full dataset every 5s. If >2s, add `django.core.cache` with 30s TTL on the state_rows result.
2. **Template complexity** — 8 templates changing at once. The shared partials (Step 6) reduce risk of divergence but the builder must test each page individually.
3. **humanize filter** — if `django.contrib.humanize` isn't in INSTALLED_APPS, `|intcomma` won't work. Check and add if needed.

# Spec 0019: Pipeline Dashboard & Control Center

**Status**: Draft
**Author**: Architect
**Date**: 2026-04-23
**Supersedes**: 0006 (Pipeline Status Dashboard, never specced)

## Problem

Operating the Lavandula pipeline requires SSH sessions, manual CLI invocations, and direct DB queries to monitor progress and start/stop processes. There is no unified view of pipeline state, no way to adjust parameters without editing commands, and no foundation for the future report interviewer application.

## Goals

Build a Django web application that serves as the operations cockpit for the Lavandula pipeline. The app has three phases, each building on the last:

### Phase 1: Pipeline Dashboard & Controls (this spec)
- Real-time visibility into pipeline state across all stages
- Start/stop/configure pipeline processes from the browser
- Model-agnostic resolver controls (codex, codex-mini, gemini, claude)

### Phase 2: Report Data Viewer (future spec)
- Browse extracted report data and styling
- View PDF pages alongside extracted text/structure
- Tag and annotate reports for training data

### Phase 3: Report Interviewer MVP (future spec)
- Structured form wizard for creating impact/annual reports
- Pulls styling and structure templates from extracted reports
- Simple text input (speech-to-text layered in later)

**This spec covers Phase 1 only.**

## Non-Goals

- Real-time streaming logs (tail -f equivalent) — too complex for Phase 1
- User management / multi-tenancy — single operator for now
- Mobile-responsive design — desktop browser only
- Report creation or editing — that's Phase 3
- Replacing the CLI tools — the dashboard calls them, doesn't replace them

## Architecture

### Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Framework | Django 5.x | Forms, ORM, admin, session handling, template engine — all needed for Phase 2-3 |
| Database | Existing PostgreSQL (lava_prod1) | Django ORM pointed at lava_impact schema via unmanaged models |
| Process management | subprocess + PID tracking | Django starts/stops pipeline CLIs, tracks PIDs in DB |
| Real-time updates | HTMX polling (5s) | Simple, no websocket infrastructure needed for Phase 1 |
| CSS | Tailwind CSS (CDN) | Fast to build, no build step |
| Charts | Chart.js (CDN) | Lightweight, good for status breakdowns |

### Django Project Structure

```
lavandula/
  dashboard/                    # Django project
    manage.py
    dashboard/                  # Django settings/urls/wsgi
      settings.py
      urls.py
      wsgi.py
    pipeline/                   # Main app
      models.py                 # Unmanaged models pointing at lava_impact tables
      views.py                  # Dashboard views
      forms.py                  # Process configuration forms
      process_manager.py        # Start/stop/status for pipeline processes
      templatetags/
        pipeline_tags.py        # Custom template filters
      templates/
        pipeline/
          base.html
          dashboard.html        # Main overview
          resolver.html         # Resolver controls + status
          crawler.html          # Crawler controls + status
          classifier.html       # Classifier controls + status
          orgs.html             # Org browser with resolver results
      static/
        pipeline/
          css/
          js/
```

### Unmanaged Models

Django models with `managed = False` that map to existing `lava_impact` tables. Django does NOT run migrations against these tables — the schema is owned by `lavandula/migrations/rds/`.

```python
class NonprofitSeed(models.Model):
    ein = models.TextField(primary_key=True)
    name = models.TextField(null=True)
    city = models.TextField(null=True)
    state = models.TextField(null=True)
    website_url = models.TextField(null=True)
    resolver_status = models.TextField(null=True)
    resolver_confidence = models.FloatField(null=True)
    resolver_method = models.TextField(null=True)
    resolver_reason = models.TextField(null=True)
    resolver_updated_at = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = 'nonprofits_seed'

class Report(models.Model):
    content_sha256 = models.TextField(primary_key=True)
    source_org_ein = models.TextField()
    classification = models.TextField(null=True)
    classification_confidence = models.FloatField(null=True)
    archived_at = models.TextField()
    file_size_bytes = models.BigIntegerField()
    page_count = models.IntegerField(null=True)
    report_year = models.IntegerField(null=True)

    class Meta:
        managed = False
        db_table = 'reports'

class CrawledOrg(models.Model):
    ein = models.TextField(primary_key=True)
    first_crawled_at = models.TextField()
    last_crawled_at = models.TextField()
    candidate_count = models.IntegerField()
    fetched_count = models.IntegerField()
    confirmed_report_count = models.IntegerField()

    class Meta:
        managed = False
        db_table = 'crawled_orgs'
```

### Process Manager

Manages pipeline processes (resolver, crawler, classifier) as subprocesses. Tracks state in a Django-managed table (the only managed model).

```python
class PipelineProcess(models.Model):
    """Managed by Django — tracks running pipeline processes."""
    name = models.CharField(max_length=50, unique=True)  # resolver, crawler, classifier
    pid = models.IntegerField(null=True)
    status = models.CharField(max_length=20)  # running, stopped, error
    started_at = models.DateTimeField(null=True)
    config_json = models.JSONField(default=dict)  # CLI args used
    last_heartbeat = models.DateTimeField(null=True)

    class Meta:
        db_table = 'pipeline_processes'
```

The process manager:
1. Builds the CLI command from form parameters
2. Spawns via `subprocess.Popen` with stdout/stderr to log files
3. Records PID in `PipelineProcess`
4. Polls PID liveness on dashboard refresh
5. Sends SIGTERM on stop

## Dashboard Views

### 1. Overview Dashboard (`/`)

Shows aggregate pipeline health at a glance:

| Section | Content |
|---------|---------|
| **Seed Pool** | Total orgs, by state, by status (NULL / resolved / unresolved / ambiguous) |
| **Resolver** | Running? Model, rate, resolved/unresolved counts this session |
| **Crawler** | Running? Orgs crawled, reports found, PDFs archived |
| **Classifier** | Running? Reports classified, by classification type |
| **Reports** | Total reports, by classification, by year |

Each section links to its detail page. HTMX polls every 5 seconds for count updates.

### 2. Resolver Controls (`/resolver/`)

**Status panel**: Current process state, PID, uptime, orgs processed, resolution rate.

**Control form**:
- State filter (dropdown: all states)
- Resolver model (dropdown: codex, codex-mini, gemini, claude)
- Limit (integer, 0 = no limit)
- Fresh only (checkbox)
- Delay between orgs (float, seconds)
- Timeout per org (integer, seconds)
- Start / Stop buttons

**Results table**: Recent resolver results with ein, name, city, status, confidence, URL, method, timestamp. Sortable, filterable.

### 3. Crawler Controls (`/crawler/`)

**Status panel**: Current process state, PID, uptime, orgs crawled this session.

**Control form**:
- Archive destination (text: s3://... or path)
- Limit (integer, 0 = no limit)
- Max workers (integer, 1-32)
- Skip encryption check (checkbox)
- Skip TLS self-test (checkbox)
- Start / Stop buttons

**Results table**: Recently crawled orgs with candidate/fetched/confirmed counts.

### 4. Classifier Controls (`/classifier/`)

**Status panel**: Current process state, PID, uptime.

**Control form**:
- Limit (integer, 0 = no limit)
- Queue size (integer)
- Gemma URL (text)
- Gemma model (text)
- Start / Stop buttons

**Results table**: Recent classifications with sha256, org EIN, classification, confidence.

### 5. Org Browser (`/orgs/`)

Paginated table of all nonprofits with:
- Filters: state, resolver_status, resolver_method
- Columns: EIN, name, city, state, URL, status, confidence, method, timestamp
- Click-through to detail view showing full resolver_reason and candidates

## Database Configuration

Django connects to the existing RDS instance using the same SSM-sourced credentials as the pipeline:

```python
# settings.py
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': '<from SSM: rds-database>',
        'USER': '<from SSM: rds-app-user>',
        'HOST': '<from SSM: rds-endpoint>',
        'PORT': '<from SSM: rds-port>',
        'OPTIONS': {
            'options': '-c search_path=lava_impact,public',
        },
    }
}
```

The `pipeline_processes` table is the only Django-managed table. It lives in `lava_impact` alongside everything else.

## Security

- **No public access**: Binds to 127.0.0.1 or internal VPC only
- **No auth in Phase 1**: Single operator, SSH-tunneled access
- **Process execution**: Only predefined CLI commands, no arbitrary shell execution
- **No secrets in browser**: SSM credentials loaded server-side only
- **CSRF protection**: Django's built-in middleware (enabled by default)

## Acceptance Criteria

### Dashboard
- AC01: Overview page loads in <2s showing seed/resolver/crawler/classifier stats
- AC02: Stats auto-refresh via HTMX every 5 seconds without full page reload
- AC03: Resolver status breakdown shows counts by method (haiku, deepseek, gemma, codex variants)
- AC04: Reports breakdown shows counts by classification and year

### Process Controls
- AC05: Resolver can be started from the UI with model selection and all CLI arguments
- AC06: Crawler can be started from the UI with archive destination and limit
- AC07: Classifier can be started from the UI with Gemma endpoint config
- AC08: Running processes can be stopped from the UI via SIGTERM
- AC09: Process status shows running/stopped/error with PID and uptime
- AC10: Starting a process that's already running shows an error, doesn't spawn a duplicate

### Org Browser
- AC11: Orgs table paginates at 50 rows per page
- AC12: Orgs filterable by state, resolver_status, resolver_method
- AC13: Org detail view shows full resolver_reason and website_candidates_json

### Integration
- AC14: Django reads from existing lava_impact tables without migrations
- AC15: Process manager correctly detects process death (PID no longer running)
- AC16: Dashboard works when no processes are running (all stopped state)

### Infrastructure
- AC17: `python manage.py runserver 0.0.0.0:8000` starts the dashboard
- AC18: Django project lives at `lavandula/dashboard/`
- AC19: No additional system dependencies beyond `pip install django psycopg2-binary`

## Traps to Avoid

1. **Don't run Django migrations on lava_impact tables** — use `managed = False` for all existing tables. Only `pipeline_processes` is Django-managed.
2. **Don't store secrets in settings.py** — load from SSM at startup, same as other pipeline tools.
3. **Don't build a SPA** — server-rendered templates + HTMX keeps it simple and fast.
4. **Don't implement websockets in Phase 1** — HTMX polling is sufficient. Django Channels is a Phase 2+ addition.
5. **Don't over-engineer process management** — subprocess + PID is adequate for single-host. Celery/supervisord is Phase 2+ if we go multi-host.
6. **Don't duplicate the `search_path` schema** — Django's `OPTIONS` sets `search_path` at connection time so all queries hit `lava_impact`.

## Consultation Log

*(To be filled after consultation)*

## Future Phases

### Phase 2: Report Data Viewer
- PDF page viewer with extracted text overlay
- Structure/styling extraction display
- Training data annotation interface
- Depends on 0014 (PDF extraction)

### Phase 3: Report Interviewer MVP
- Structured form wizard (Django forms)
- Section-by-section question flow
- Template styling from extracted reports
- Text input initially, speech-to-text layered in
- AI-assisted suggestions as training data grows

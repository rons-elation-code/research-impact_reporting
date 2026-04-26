# Spec 0019: Pipeline Orchestrator & Control Center

**Status**: Draft
**Author**: Architect
**Date**: 2026-04-26 (revised from 2026-04-23 draft)
**Supersedes**: 0006 (Pipeline Status Dashboard, never specced)

## Problem

Operating the Lavandula pipeline requires SSH sessions, manual CLI invocations, hand-written bash scripts, and direct DB queries to monitor progress and start/stop processes. Today's workflow:

1. SSH into cloud1 or cloud2
2. Write a bash loop to seed→resolve→crawl each state
3. `nohup` it and hope it works
4. SSH back in to `tail -f` log files for status
5. Repeat for each host

This doesn't scale. With 50 states, multiple hosts, and the extraction pipeline coming, we need a unified system that queues work, runs it unattended, and shows progress without SSH.

## Goals

Build a Django web application that serves as the operations cockpit for the Lavandula pipeline. The key addition over the 2026-04-23 draft is a **job orchestrator** — a state machine that automates the seed→resolve→crawl pipeline per state, replacing the manual bash scripts.

The app is delivered in three iterations:

### Iteration 1: Orchestrator & Operations Cockpit (this spec)
- **Job queue**: Define work units (seed state X, resolve state X, crawl state X) that execute automatically in sequence
- **Real-time visibility** into pipeline state across all stages
- **Start/stop/configure** every pipeline process from the browser
- **Multi-host awareness**: jobs track which host they run on
- **Org and report browsers** with filtering

### Iteration 2: Report Data Viewer (future spec)
- Browse extracted report data and styling
- View PDF pages alongside extracted text/structure
- Tag and annotate reports for training data

### Iteration 3: Report Interviewer MVP (future spec)
- Structured form wizard for creating impact/annual reports
- Domain-specific interview questions derived from corpus analysis
- Pulls styling and structure templates from extracted reports

**This spec covers Iteration 1 only.**

## Non-Goals

- User management / multi-tenancy — single operator for now
- Mobile-responsive design — desktop browser only
- Report creation or editing — that's Iteration 3
- Remote agent execution — jobs run on the dashboard's host via subprocess; multi-host execution is SSH-triggered from the dashboard host or manually started on remote hosts
- Replacing the CLI tools — the dashboard calls them, doesn't replace them

## Architecture

### Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Framework | Django 5.x | Forms, ORM, admin, session handling, template engine — all needed for Iterations 2-3 |
| Database | Existing PostgreSQL (lava_prod1) | Django ORM pointed at lava_impact schema via unmanaged models |
| Job execution | subprocess + PID tracking | Django starts pipeline CLIs, tracks PIDs in DB |
| Real-time updates | HTMX polling (5s) | Simple, no websocket infrastructure needed |
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
      models.py                 # Unmanaged models + managed job/process tables
      views.py                  # Dashboard views
      forms.py                  # Job + process configuration forms
      orchestrator.py           # Job state machine + runner
      process_manager.py        # Start/stop/status for pipeline processes
      templatetags/
        pipeline_tags.py        # Custom template filters
      management/
        commands/
          run_orchestrator.py   # `manage.py run_orchestrator` daemon
      templates/
        pipeline/
          base.html
          dashboard.html        # Main overview
          jobs.html             # Job queue + history
          job_detail.html       # Single job progress
          resolver.html         # Resolver controls + status
          crawler.html          # Crawler controls + status
          classifier.html       # Classifier controls + status
          orgs.html             # Org browser
          reports.html          # Reports browser
      static/
        pipeline/
          css/
          js/
```

### Job Orchestrator (New)

The orchestrator is the core addition. It manages **jobs** — units of work that move through a state machine.

#### Job Model

```python
class Job(models.Model):
    """A unit of pipeline work. Django-managed table."""
    id = models.AutoField(primary_key=True)
    state_code = models.CharField(max_length=2)  # e.g., "NY", "MA"
    phase = models.CharField(max_length=20)       # seed, resolve, crawl
    status = models.CharField(max_length=20)      # pending, running, completed, failed, cancelled
    host = models.CharField(max_length=100, default="localhost")
    pid = models.IntegerField(null=True)
    config_json = models.JSONField(default=dict)  # CLI args
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    exit_code = models.IntegerField(null=True)
    log_file = models.CharField(max_length=255, null=True)
    error_message = models.TextField(null=True)
    
    # Progress tracking (updated periodically by log parser or DB query)
    progress_current = models.IntegerField(default=0)
    progress_total = models.IntegerField(null=True)
    
    class Meta:
        db_table = 'jobs'
        indexes = [
            models.Index(fields=['status', 'phase']),
            models.Index(fields=['state_code', 'phase']),
        ]
```

#### State Machine

```
pending → running → completed
                  → failed → pending (retry)
pending → cancelled
```

- **pending**: Queued, waiting for a runner slot
- **running**: Subprocess active, PID tracked
- **completed**: Process exited 0
- **failed**: Process exited non-zero or was killed; can be retried (creates new pending job)
- **cancelled**: Operator cancelled before or during execution

#### Phase Sequencing

When the operator submits a "Run State" request (e.g., "Run MA"), the orchestrator creates up to 3 jobs:

```
Job 1: seed MA   (pending)
Job 2: resolve MA (pending, depends_on=Job 1)
Job 3: crawl MA  (pending, depends_on=Job 2)
```

A job with `depends_on` only becomes eligible for execution when its dependency reaches `completed`. If a dependency fails, dependent jobs stay pending (operator decides whether to retry or cancel).

The dependency is stored as a foreign key:

```python
depends_on = models.ForeignKey('self', null=True, on_delete=models.SET_NULL, related_name='dependents')
```

#### Runner

The orchestrator runner is a Django management command (`manage.py run_orchestrator`) that:

1. Polls for eligible jobs every 10 seconds (status=pending, no unfinished dependencies, host=localhost)
2. Picks the oldest eligible job
3. Builds the CLI command from `config_json` using the command allowlist
4. Spawns via `subprocess.Popen`, records PID
5. Monitors the process (non-blocking poll)
6. On exit: updates status to completed/failed, records exit_code
7. Checks for next eligible job

Concurrency limit: **1 job at a time per phase** (one seed, one resolve, one crawl can run simultaneously). This matches the existing pipeline architecture where each phase has its own DB write path.

#### "Run State" Convenience

The most common operation is "seed + resolve + crawl state X." The UI provides a single form:

- State code (dropdown or multi-select)
- Phases to run (checkboxes: seed, resolve, crawl — all checked by default)
- Configuration overrides (LLM model, brave QPS, consumer threads, crawl concurrency)

Submitting creates the chained jobs automatically. For multi-state submissions (e.g., "Run MA, VA, FL, MD"), jobs are created for all states. The runner processes them in submission order, respecting per-state phase dependencies.

### Unmanaged Models

Django models with `managed = False` that map to existing `lava_impact` tables. Django does NOT run migrations against these tables — the schema is owned by `lavandula/migrations/rds/`.

```python
class NonprofitSeed(models.Model):
    ein = models.TextField(primary_key=True)
    name = models.TextField(null=True)
    city = models.TextField(null=True)
    state = models.TextField(null=True)
    website_url = models.TextField(null=True)
    website_candidates_json = models.TextField(null=True)
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
    source_url_redacted = models.TextField(null=True)
    classification = models.TextField(null=True)
    classification_confidence = models.FloatField(null=True)
    archived_at = models.TextField()
    file_size_bytes = models.BigIntegerField()
    page_count = models.IntegerField(null=True)
    report_year = models.IntegerField(null=True)
    first_page_text = models.TextField(null=True)

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

For ad-hoc process execution outside the job queue (e.g., one-off classifier run, manual crawl). Manages pipeline processes as subprocesses with PID tracking.

```python
class PipelineProcess(models.Model):
    """Managed by Django — tracks running ad-hoc pipeline processes."""
    name = models.CharField(max_length=50, unique=True)  # resolver, crawler, classifier
    pid = models.IntegerField(null=True)
    status = models.CharField(max_length=20)  # running, stopped, error
    started_at = models.DateTimeField(null=True)
    config_json = models.JSONField(default=dict)
    last_heartbeat = models.DateTimeField(null=True)
    log_file = models.CharField(max_length=255, null=True)

    class Meta:
        db_table = 'pipeline_processes'

class PipelineAuditLog(models.Model):
    """Managed by Django — audit trail for all operator actions."""
    action = models.CharField(max_length=20)  # start, stop, config_change, job_create, job_cancel
    process_name = models.CharField(max_length=50)
    parameters = models.JSONField(default=dict)
    source_ip = models.GenericIPAddressField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pipeline_audit_log'
```

The process manager:
1. Acquires a row-level lock (`select_for_update`) on the `PipelineProcess` row to prevent double-start races
2. Builds the CLI command from form parameters using a strict allowlist (see Command Mapping below)
3. Spawns via `subprocess.Popen` with `cwd` set to the project root and `PYTHONPATH` explicitly set, stdout/stderr redirected to `lavandula/logs/dashboard/{name}_{timestamp}.log`
4. Records PID and log file path in `PipelineProcess`
5. On dashboard refresh, verifies PID liveness AND validates `/proc/{pid}/cmdline` matches the expected command to guard against PID reuse
6. On stop: sends SIGTERM, waits up to 10 seconds, then SIGKILL if still alive
7. On dashboard startup: scans all `running` rows, validates each PID is still alive and matches; marks stale entries as `stopped`

### Command Mapping

Each pipeline phase maps to a fixed CLI command with a strict parameter allowlist. No arbitrary shell execution — the process manager constructs `argv` arrays, never shell strings.

```python
COMMAND_MAP = {
    "seed": {
        "cmd": ["python3", "-m", "lavandula.nonprofits.tools.seed_enumerate"],
        "params": {
            "states": {"type": "text", "pattern": r"^[A-Z]{2}$", "flag": "--states"},
            "target": {"type": "int", "min": 1, "max": 999999, "flag": "--target"},
            "ntee_majors": {"type": "text", "flag": "--ntee-majors"},
            "revenue_min": {"type": "int", "min": 0, "flag": "--revenue-min"},
            "revenue_max": {"type": "int", "min": 0, "flag": "--revenue-max"},
        },
    },
    "resolver": {
        "cmd": ["python3", "-m", "lavandula.nonprofits.tools.pipeline_resolve"],
        "params": {
            "state": {"type": "choice", "choices": "US_STATES", "flag": "--state"},
            "llm_url": {"type": "text", "pattern": r"^https?://", "flag": "--llm-url"},
            "llm_model": {"type": "text", "flag": "--llm-model"},
            "llm_api_key_ssm": {"type": "text", "flag": "--llm-api-key-ssm"},
            "brave_qps": {"type": "float", "min": 0.1, "max": 50.0, "flag": "--brave-qps"},
            "search_parallelism": {"type": "int", "min": 1, "max": 32, "flag": "--search-parallelism"},
            "consumer_threads": {"type": "int", "min": 1, "max": 16, "flag": "--consumer-threads"},
            "limit": {"type": "int", "min": 0, "max": 999999, "flag": "--limit"},
            "fresh_only": {"type": "bool", "flag": "--fresh-only"},
        },
    },
    "crawler": {
        "cmd": ["python3", "-m", "lavandula.reports.crawler"],
        "params": {
            "archive": {"type": "text", "pattern": r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9](/[a-zA-Z0-9._-]+)*$", "flag": "--archive"},
            "async": {"type": "bool", "flag": "--async"},
            "limit": {"type": "int", "min": 0, "max": 999999, "flag": "--limit"},
            "max_concurrent_orgs": {"type": "int", "min": 1, "max": 500, "flag": "--max-concurrent-orgs"},
            "max_download_workers": {"type": "int", "min": 1, "max": 100, "flag": "--max-download-workers"},
            "skip_encryption_check": {"type": "bool", "flag": "--skip-encryption-check"},
        },
    },
    "classifier": {
        "cmd": ["python3", "-m", "lavandula.nonprofits.tools.pipeline_classify"],
        "params": {
            "llm_url": {"type": "text", "pattern": r"^https?://", "flag": "--llm-url"},
            "llm_model": {"type": "text", "flag": "--llm-model"},
            "llm_api_key_ssm": {"type": "text", "flag": "--llm-api-key-ssm"},
            "limit": {"type": "int", "min": 0, "max": 999999, "flag": "--limit"},
        },
    },
}
```

## Dashboard Views

### 1. Overview Dashboard (`/`)

Shows aggregate pipeline health at a glance:

| Section | Content |
|---------|---------|
| **Job Queue** | Active/pending/completed jobs. Current state being processed. Link to job queue page |
| **Seed Pool** | Total orgs by state, by resolver_status. Counts per state |
| **Resolver** | Running? Resolved/unresolved counts, resolution rate |
| **Crawler** | Running? Orgs crawled, reports found |
| **Classifier** | Running? Reports classified, by classification type |
| **Reports** | Total reports, by classification, by year |

HTMX polls every 5 seconds for count updates.

### 2. Job Queue (`/jobs/`)

The primary operational view. Shows:

**Active jobs**: Currently running jobs with real-time progress bars (progress_current / progress_total), phase, state, host, elapsed time.

**Pending jobs**: Queued jobs waiting for dependencies or runner slots. Shows dependency chain.

**Run State form**: 
- State code (multi-select dropdown for all 50 states + territories)
- Phases (checkboxes: seed, resolve, crawl — all checked by default)
- Configuration panel (expandable): LLM model, brave QPS, consumer threads, etc.
- "Queue" button creates the chained jobs

**Job history**: Completed/failed/cancelled jobs with duration, exit code, link to log.

### 3. Job Detail (`/jobs/<id>/`)

Single job view:
- Status, phase, state, host, PID
- Progress bar
- Configuration used
- Last 100 lines of log file (HTMX-refreshed)
- Cancel button (if pending/running)
- Retry button (if failed)

### 4. Resolver Controls (`/resolver/`)

**Status panel**: Current job or ad-hoc process state, PID, uptime, orgs processed.

**Ad-hoc form** (for one-off runs outside the job queue):
- State filter, LLM model, brave QPS, consumer threads, limit, fresh only
- Start / Stop buttons

**Results table**: Recent resolver results with EIN, name, city, status, confidence, URL, method, timestamp.

### 5. Crawler Controls (`/crawler/`)

**Status panel**: Current job or ad-hoc process state.

**Ad-hoc form**:
- Archive destination, limit, concurrency settings
- Start / Stop buttons

**Results tables**:
- Recently crawled orgs: org, candidate count, fetched count, confirmed reports, timestamp
- Recently archived reports: filename (URL basename), org, file size, classification, timestamp

### 6. Classifier Controls (`/classifier/`)

**Status panel**: Current job or ad-hoc process state.

**Ad-hoc form**:
- LLM model, limit
- Start / Stop buttons

**Results table**: Recent classifications with filename, org, classification, confidence, timestamp.

### 7. Org Browser (`/orgs/`)

Paginated table of all nonprofits:
- Filters: state, resolver_status, resolver_method
- Columns: EIN, name, city, state, URL, status, confidence, method, timestamp
- Click-through to detail view showing full resolver_reason and candidates

### 8. Reports Browser (`/reports/`)

Paginated table of all classified reports:
- Filters: org (EIN or name search), classification, report_year, archived_at range
- Columns: filename (URL basename), org, classification, confidence, year, size, archived_at
- Click-through to detail: full source URL, SHA, first_page_text, PDF metadata
- Download link via signed S3 URL (5-minute expiry)

## Database Configuration

Django connects to the existing RDS instance using the same SSM-sourced credentials as the pipeline.

```python
# settings.py
from lavandula.common.secrets import get_secret

DATABASES = {
    'default': {  # lava_dashboard schema — Django managed tables (jobs, processes, audit)
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': get_secret('rds-database'),
        'USER': get_secret('rds-dashboard-user'),
        'HOST': get_secret('rds-endpoint'),
        'PORT': get_secret('rds-port'),
        'OPTIONS': {
            'options': '-c search_path=lava_dashboard,public',
        },
    },
    'pipeline': {  # lava_impact schema — read-only pipeline data
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': get_secret('rds-database'),
        'USER': get_secret('rds-app-user'),
        'HOST': get_secret('rds-endpoint'),
        'PORT': get_secret('rds-port'),
        'OPTIONS': {
            'options': '-c search_path=lava_impact,public',
        },
    },
}
```

**Schema separation**: Django's managed tables (`jobs`, `pipeline_processes`, `pipeline_audit_log`, `django_migrations`, `auth_user`, etc.) live in `lava_dashboard` schema. Pipeline data stays in `lava_impact`. A custom database router directs reads for unmanaged models to the `pipeline` DB alias.

## Security

- **Network binding**: Binds to `127.0.0.1:8000` only, accessed via SSH tunnel or Tailscale
- **Authentication**: Django's built-in `AuthenticationMiddleware` + `LoginRequiredMixin` on all views. Single superuser created via `manage.py createsuperuser`
- **DEBUG = False**: Hardcoded in production settings
- **Process execution**: Only predefined CLI commands via `COMMAND_MAP` allowlist. Commands built as `argv` arrays (never shell strings)
- **No secrets via CLI flags**: Pipeline tools load their own credentials from SSM/environment. The dashboard never passes secrets as command-line arguments
- **CSRF protection**: Django's built-in CSRF middleware. HTMX configured to include CSRF token in request headers
- **Input validation**: All form parameters validated server-side against `COMMAND_MAP` type/range/pattern definitions
- **Log viewing safety**: Log file paths stored in DB at creation, validated via `os.path.realpath` against allowed directory before reading
- **Audit logging**: All job creation, cancellation, and process start/stop actions logged to `PipelineAuditLog`
- **Process limits**: Maximum 1 job per phase running simultaneously. Ad-hoc processes also limited to 1 per pipeline stage

## Acceptance Criteria

### Job Orchestrator
- AC01: "Run State" form creates chained seed→resolve→crawl jobs with correct dependencies
- AC02: Orchestrator runner picks and executes eligible jobs automatically
- AC03: Job with unfinished dependency stays pending until dependency completes
- AC04: Failed job can be retried from the UI (creates new pending job)
- AC05: Running job can be cancelled from the UI (SIGTERM → 10s → SIGKILL)
- AC06: Multi-state submission creates jobs for all selected states
- AC07: Runner respects concurrency limit (1 job per phase)
- AC08: Job progress updates in real-time via HTMX (progress_current/progress_total)

### Dashboard
- AC09: Overview page loads showing job queue + seed/resolver/crawler/classifier stats
- AC10: Stats auto-refresh via HTMX every 5 seconds without full page reload
- AC11: Resolver status breakdown shows counts by `resolver_method`
- AC12: Reports breakdown shows counts by classification and year

### Process Controls
- AC13: Each pipeline stage can be started ad-hoc from the UI with configurable parameters
- AC14: Running processes can be stopped from the UI
- AC15: Process status shows running/stopped/error with PID and uptime
- AC16: Starting a process that's already running shows an error (enforced via `select_for_update`)
- AC17: Each process/job detail page shows last 100 lines of log file

### Org Browser
- AC18: Orgs table paginates at 50 rows per page
- AC19: Orgs filterable by state, resolver_status, resolver_method
- AC20: Org detail view shows full resolver_reason and website_candidates_json

### Reports Browser
- AC21: Reports table paginates at 50 rows per page
- AC22: Reports filterable by org, classification, report_year, archived_at range
- AC23: Reports table displays filename (URL basename) as primary identifier
- AC24: Report detail view exposes signed S3 download URL (5-minute expiry)

### Integration
- AC25: Django reads from existing lava_impact tables without running migrations against them
- AC26: Process manager correctly detects process death (PID gone or cmdline mismatch)
- AC27: Dashboard works when no processes are running (all stopped state)
- AC28: Dashboard startup cleans up stale `running` rows from previous sessions
- AC29: All actions logged to `PipelineAuditLog` with timestamp, parameters, and source IP

### Infrastructure
- AC30: `python manage.py runserver 127.0.0.1:8000` starts the dashboard
- AC31: `python manage.py run_orchestrator` starts the job runner daemon
- AC32: Django project lives at `lavandula/dashboard/`
- AC33: No additional system dependencies beyond `pip install django psycopg2-binary`
- AC34: Login required to access any dashboard page

## Testing Strategy

### Unit Tests
- **Orchestrator**: Job creation with dependencies, state machine transitions, eligibility logic, concurrency limits
- **Command builder**: Verify `COMMAND_MAP` produces correct `argv` arrays. Verify invalid parameters rejected
- **Process state machine**: State transitions, stale PID cleanup, PID reuse detection
- **Log tail**: Reading last N lines from log files, including empty/missing/still-being-written

### View Tests
- **Dashboard views**: Django test client verifies pages render with correct context
- **Form validation**: Valid/invalid data, server-side validation against `COMMAND_MAP`
- **HTMX fragments**: Polling endpoints return HTML fragments, not full pages

### Integration Tests
- **Unmanaged models**: Django can read from `nonprofits_seed`, `reports`, `crawled_orgs`
- **Job lifecycle**: Create job → runner picks it → subprocess completes → status updates
- **Dependency chain**: Seed job completes → resolve job becomes eligible → crawl follows
- **Process lifecycle**: Start dummy process, verify PID tracking, stop, verify cleanup

## Traps to Avoid

1. **Don't run Django migrations on lava_impact tables** — use `managed = False`. Only job/process/audit tables are Django-managed
2. **Don't store secrets in settings.py** — load from SSM via `lavandula.common.secrets`
3. **Don't build a SPA** — server-rendered templates + HTMX
4. **Don't implement websockets** — HTMX polling is sufficient for Iteration 1
5. **Don't over-engineer job scheduling** — the runner is a simple poll loop, not Celery/Airflow
6. **Don't build CLI commands as shell strings** — always `argv` arrays
7. **Don't forget CSRF + HTMX** — configure `hx-headers` on body tag
8. **Don't make the orchestrator mandatory** — ad-hoc process controls still work independently for quick one-off runs

## Future Iterations

### Iteration 2: Report Data Viewer
- PDF page viewer with extracted text overlay
- Structure/styling extraction display (Docling output)
- Training data annotation interface
- Depends on extraction pipeline (Docling host)

### Iteration 3: Report Interviewer MVP
- Domain-specific interview questions derived from corpus analysis per NTEE subcode
- Structured form wizard (Django forms)
- Suggested data formatting based on peer reports
- Template styling from extracted reports
- Text input initially, speech-to-text layered in
- The core IP: turning the nonprofit domain vocabulary into intelligent report authoring

## Consultation Log

### Round 1 — Spec Review (2026-04-23)

**Gemini (APPROVE)**: Log viewing, PID verification, schema separation, concurrency locking, CSRF+HTMX — all addressed.

**Codex (REQUEST_CHANGES)**: Process lifecycle, command mapping, managed table strategy, session metrics, security, testing — all addressed.

### Round 2 — Red Team Security Review (2026-04-23)

**Gemini Red Team (REQUEST_CHANGES)**:
- CRITICAL: Authentication (fixed: LoginRequiredMixin), DB privilege separation (fixed: separate schemas)
- HIGH: Path traversal in logs (fixed: stored paths + realpath validation), audit logging (fixed: PipelineAuditLog)
- MEDIUM: DEBUG=False (fixed), resource exhaustion (fixed: process limits)
- LOW: S3 regex, cleartext secrets — mitigated

### Round 3 — Revised Spec Review (pending)

Orchestrator additions (job queue, state machine, "Run State" form, runner daemon) require re-review.

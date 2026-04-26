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
- Remote agent execution — in Iteration 1, remote jobs are metadata-only placeholders. The `host` field records where a job ran, but the runner only executes jobs on localhost. Jobs for remote hosts are created via that host's own orchestrator instance (each host runs its own `manage.py run_orchestrator`). All hosts share the same RDS job table, so the dashboard shows jobs from all hosts regardless of where they execute.
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
    state_code = models.CharField(max_length=2, null=True)  # e.g., "NY", "MA"; null for global phases (crawl)
    phase = models.CharField(max_length=20)       # canonical enum: seed | resolve | crawl | classify
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

The crawl phase is **global** — it operates on the full pool of resolved orgs regardless of state. It is not chained to per-state seed/resolve jobs.

When the operator submits a "Run State" request (e.g., "Run MA"), the orchestrator creates 2 chained jobs:

```
Job 1: seed MA    (pending, state_code="MA")
Job 2: resolve MA (pending, state_code="MA", depends_on=Job 1)
```

Crawl jobs are submitted independently via the Crawler Controls page (`/crawler/`). A crawl job has `state_code=NULL` and no `depends_on` — it runs against the global pool of resolved orgs.

A job with `depends_on` only becomes eligible for execution when its dependency reaches `completed`. If a dependency fails, dependent jobs stay pending (operator decides whether to retry or cancel).

The dependency is stored as a foreign key:

```python
depends_on = models.ForeignKey('self', null=True, on_delete=models.SET_NULL, related_name='dependents')
```

#### State Machine — Retry and Cancel Dependency Rules

When the state machine transitions jobs, dependent job wiring is updated as follows:

- **Retry (failed → new pending)**: A new pending job is created as the replacement. Any downstream jobs whose `depends_on` points to the failed job are automatically rewired to point to the new job.
- **Cancel running job**: The job is sent SIGTERM (then SIGKILL after 10 s). All downstream dependents (jobs with `depends_on` pointing to this job) are also cancelled immediately.
- **Cancel pending job**: The job is marked `cancelled`. All downstream dependents are also cancelled (cascade).

These rules ensure the dependency graph never contains dangling references to terminal (failed/cancelled) jobs.

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

The most common per-state operation is "seed + resolve state X." The UI provides a single form:

- State code (dropdown or multi-select)
- Phases to run (checkboxes: seed, resolve — all checked by default)
- Configuration overrides (LLM model, brave QPS, consumer threads)

Submitting creates the seed→resolve chain automatically. For multi-state submissions (e.g., "Run MA, VA, FL, MD"), jobs are created for all states. The runner processes them in submission order, respecting per-state phase dependencies.

**Crawl is managed separately** on the Crawler Controls page, since it runs against the global resolved-org pool and is not tied to any single state.

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
    name = models.CharField(max_length=50, unique=True)  # canonical: resolve, crawl, classify
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
3. Spawns via `subprocess.Popen` with `cwd` set to the project root and `PYTHONPATH` explicitly set, stdout/stderr redirected to `lavandula/logs/dashboard/{name}_{timestamp}.log`. Pipeline commands are launched with `os.setpgrp()` as `preexec_fn` so that SIGTERM can target the entire process group.
4. Records PID and log file path in `PipelineProcess`
5. On dashboard refresh, verifies PID liveness AND validates `/proc/{pid}/cmdline` matches the expected command to guard against PID reuse
6. On stop: sends SIGTERM, waits up to 10 seconds, then SIGKILL if still alive
7. On dashboard startup: scans all `running` rows, validates each PID is still alive and matches; marks stale entries as `stopped`
8. **`Popen` OSError / FileNotFoundError**: if the subprocess cannot be spawned (missing executable, permission denied), the job or process row is immediately marked `failed` with the exception message stored in `error_message`; PID remains null.
9. **Immediate exit (< 1 s)**: if the process exits within one poll cycle after launch, it is detected on the next poll and marked `failed` (or `completed` if exit code is 0). PID is recorded from the `Popen` object before the poll.
10. **DB update failure during state transition**: the status write is retried once after a short delay. If the retry also fails, the error is logged to stderr and the subprocess is killed to avoid an untracked orphan process.
11. **Orphaned children**: because pipeline commands use `os.setpgrp()`, the orchestrator can send SIGTERM to the entire process group (`os.killpg`) rather than just the top-level PID, ensuring child processes do not outlive the parent.

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
    "resolve": {
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
    "crawl": {
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
    "classify": {
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

**Managed table strategy**: Django migrations run only against the `lava_dashboard` schema via `manage.py migrate` (the `default` alias). Auth, session, and content_type framework tables are in scope and land in `lava_dashboard`. The database router must prevent any write operation (including `migrate`) from touching the `pipeline` DB alias:

```python
class PipelineRouter:
    """Route unmanaged pipeline models to the read-only pipeline DB alias."""

    pipeline_app = 'pipeline'

    def db_for_read(self, model, **hints):
        if not model._meta.managed:
            return 'pipeline'
        return 'default'

    def db_for_write(self, model, **hints):
        if not model._meta.managed:
            raise RuntimeError(
                f"Write blocked: {model._meta.label} is an unmanaged model; "
                "lava_impact schema is owned by lavandula/migrations/rds/."
            )
        return 'default'

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Never run migrations on the pipeline alias
        if db == 'pipeline':
            return False
        return True
```

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
- **Session cookie flags**: `SESSION_COOKIE_HTTPONLY = True` and `SESSION_COOKIE_SECURE = True` (the dashboard is accessed via HTTPS-terminated SSH tunnel or Tailscale).
- **HTMX CSRF enforcement**: All HTMX POST/PUT/DELETE endpoints require a valid CSRF token. The base template sets `hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'` on the `<body>` tag so every HTMX mutation includes the token automatically.
- **Log output escaping**: Log content and `error_message` values rendered in templates rely on Django's default auto-escaping. The `|safe` filter must never be applied to log content or any operator-controlled text.

## Acceptance Criteria

### Job Orchestrator
- AC01: "Run State" form creates chained seed→resolve jobs with correct dependencies; crawl jobs are submitted separately via the Crawler Controls page
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
- **Dependency chain**: Seed job completes → resolve job becomes eligible → resolve completes → no automatic crawl enqueued
- **Process lifecycle**: Start dummy process, verify PID tracking, stop, verify cleanup
- **Retry rewiring**: Failed job is retried → new pending job is created → dependents are rewired to new job → dependency chain completes successfully
- **Cancel cascade**: Cancel a running job → downstream dependent jobs are also cancelled; same behaviour when cancelling a pending job with dependents
- **Concurrent submissions**: Two "Run State" requests for the same state submitted in rapid succession → second submission is either rejected with an error or queued independently (no duplicate dependency violation)
- **Crawl independence**: A crawl job is created with `state_code=NULL` and no `depends_on`; it becomes eligible immediately without waiting for any seed/resolve jobs

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

### Round 3 — Revised Spec Review (2026-04-26)

**Codex (REQUEST_CHANGES)** — 8 issues addressed:

1. **Phase naming inconsistency** (MEDIUM): `COMMAND_MAP` keys `"resolver"`, `"crawler"`, `"classifier"` did not match the canonical `phase` enum (`seed`, `resolve`, `crawl`, `classify`). Renamed keys to match. `PipelineProcess.name` comment updated. `state_code` made nullable for global phases.
2. **Crawl is not state-scoped** (HIGH): Crawl phase operates on the global resolved-org pool, not per-state. "Run State" now creates seed→resolve chains only. Crawl is submitted independently via the Crawler Controls page. `state_code=NULL` and no `depends_on` for crawl jobs. AC01 and dependency-chain integration test updated.
3. **Retry/cancel dependency behavior underspecified** (HIGH): Explicit rules added to the State Machine section: retry rewires dependents to the new job; cancelling a running or pending job cascades to all downstream dependents.
4. **Multi-host scope ambiguous** (MEDIUM): Non-Goals clarified that remote jobs are metadata-only placeholders in Iteration 1. Each host runs its own `run_orchestrator`; all hosts share the same RDS job table so the dashboard shows all jobs.
5. **Managed table strategy incomplete** (MEDIUM): Database Configuration section now specifies that `manage.py migrate` targets `lava_dashboard` only. Auth/session/content_type tables land there. `PipelineRouter` code example added with hard `RuntimeError` on any write attempt against the `pipeline` alias.
6. **Subprocess failure handling underspecified** (HIGH): Process Manager section extended with four explicit failure scenarios: `Popen` OSError/FileNotFoundError, immediate exit (< 1 s), DB update failure during state transition (retry-once then kill), and orphaned children via `os.setpgrp()` / `os.killpg`.
7. **Security gaps** (MEDIUM): Added `SESSION_COOKIE_HTTPONLY`, `SESSION_COOKIE_SECURE`, explicit HTMX CSRF enforcement requirement, and prohibition on `|safe` filter for log/error content.
8. **Integration test coverage gaps** (LOW): Four new integration test cases added: retry rewiring, cancel cascade, concurrent submissions, and crawl independence.

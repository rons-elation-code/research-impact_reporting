# Spec 0019: Pipeline Dashboard & Control Center

**Status**: Review
**Author**: Architect
**Date**: 2026-04-23
**Supersedes**: 0006 (Pipeline Status Dashboard, never specced)

## Problem

Operating the Lavandula pipeline requires SSH sessions, manual CLI invocations, and direct DB queries to monitor progress and start/stop processes. There is no unified view of pipeline state, no way to adjust parameters without editing commands, and no foundation for the future report interviewer application.

## Goals

Build a Django web application that serves as the operations cockpit for the Lavandula pipeline. This spec covers operating the **entire** pipeline (seed, resolver, crawler, classifier) from the browser — monitoring all stages and controlling all processes. Future iterations layer on data viewing and report authoring features; they do not add new pipeline stages.

The app is delivered in three iterations, each building on the last:

### Iteration 1: Operations Cockpit (this spec)
- Real-time visibility into pipeline state across all stages (seed, resolver, crawler, classifier)
- Start/stop/configure every pipeline process from the browser
- Model-agnostic resolver controls (codex, codex-mini, gemini, claude)
- Org browser with filtering and resolver-result detail

### Iteration 2: Report Data Viewer (future spec)
- Browse extracted report data and styling
- View PDF pages alongside extracted text/structure
- Tag and annotate reports for training data

### Iteration 3: Report Interviewer MVP (future spec)
- Structured form wizard for creating impact/annual reports
- Pulls styling and structure templates from extracted reports
- Simple text input (speech-to-text layered in later)

**This spec covers Iteration 1 only.** "Phase" terminology is avoided to prevent confusion with pipeline *stages*.

## Non-Goals

- User management / multi-tenancy — single operator for now
- Mobile-responsive design — desktop browser only
- Report creation or editing — that's Iteration 3
- Replacing the CLI tools — the dashboard calls them, doesn't replace them

## Architecture

### Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Framework | Django 5.x | Forms, ORM, admin, session handling, template engine — all needed for Iterations 2-3 |
| Database | Existing PostgreSQL (lava_prod1) | Django ORM pointed at lava_impact schema via unmanaged models |
| Process management | subprocess + PID tracking | Django starts/stops pipeline CLIs, tracks PIDs in DB |
| Real-time updates | HTMX polling (5s) | Simple, no websocket infrastructure needed for Iteration 1 |
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
    first_page_text = models.TextField(null=True)  # needed by Iteration 2 viewer

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

class PipelineAuditLog(models.Model):
    """Managed by Django — audit trail for all operator actions."""
    action = models.CharField(max_length=20)  # start, stop, config_change
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
4. Records PID and full command line in `PipelineProcess`
5. On dashboard refresh, verifies PID liveness AND validates `/proc/{pid}/cmdline` matches the expected command to guard against PID reuse
6. On stop: sends SIGTERM, waits up to 10 seconds, then SIGKILL if still alive
7. On dashboard startup: scans all `running` rows, validates each PID is still alive and matches; marks stale entries as `stopped`

**Process states**: `running` (PID alive and verified), `stopped` (graceful shutdown or stale PID cleanup), `error` (process exited with non-zero code or was killed)

### Command Mapping

Each pipeline process maps to a fixed CLI command with a strict parameter allowlist. No arbitrary shell execution — the process manager constructs `argv` arrays, never shell strings.

```python
COMMAND_MAP = {
    "resolver": {
        "cmd": ["python3", "-m", "lavandula.nonprofits.tools.cli_resolve"],
        "params": {
            "state": {"type": "choice", "choices": [US_STATES], "flag": "--state"},
            "resolver": {"type": "choice", "choices": ["codex", "codex-mini", "gemini", "claude"], "flag": "--resolver"},
            "limit": {"type": "int", "min": 0, "max": 100000, "flag": "--limit"},
            "fresh_only": {"type": "bool", "flag": "--fresh-only"},
            "delay": {"type": "float", "min": 0.0, "max": 60.0, "flag": "--delay"},
            "timeout": {"type": "int", "min": 10, "max": 600, "flag": "--timeout"},
        },
    },
    "crawler": {
        "cmd": ["python3", "-m", "lavandula.reports.crawler"],
        "params": {
            "archive": {"type": "text", "pattern": r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9](/[a-zA-Z0-9._-]+)*$", "flag": "--archive"},
            "limit": {"type": "int", "min": 0, "max": 100000, "flag": "--limit"},
            "max_workers": {"type": "int", "min": 1, "max": 32, "flag": "--max-workers"},
            "skip_encryption_check": {"type": "bool", "flag": "--skip-encryption-check"},
            "skip_tls_self_test": {"type": "bool", "flag": "--skip-tls-self-test"},
        },
    },
    "classifier": {
        "cmd": ["python3", "-m", "lavandula.reports.tools.classify_null"],
        "params": {
            "limit": {"type": "int", "min": 0, "max": 100000, "flag": "--limit"},
            "max_workers": {"type": "int", "min": 1, "max": 32, "flag": "--max-workers"},
            "re_classify": {"type": "bool", "flag": "--re-classify"},
        },
    },
}
```

### Log Viewing

Each process writes stdout/stderr to `lavandula/logs/dashboard/{name}_{timestamp}.log`. The detail view for each process shows the **last 100 lines** of the current log file, refreshed via HTMX along with other status data. This provides basic ops visibility without the complexity of real-time streaming.

## Dashboard Views

### 1. Overview Dashboard (`/`)

Shows aggregate pipeline health at a glance:

| Section | Content |
|---------|---------|
| **Seed Pool** | Read-only stats: total orgs, by state, by status (NULL / resolved / unresolved / ambiguous). Not a running stage — seed enumeration is a bounded one-off batch (spec 0001). No Start/Stop control in Iteration 1. |
| **Resolver** | Running? Model, resolved/unresolved counts since `started_at` |
| **Crawler** | Running? Orgs crawled, reports found since `started_at` |
| **Classifier** | Running? Reports classified, by classification type |
| **Reports** | Total reports, by classification, by year. Links to Reports Browser for row-level detail. |

"Session" metrics are DB deltas: count rows where the relevant timestamp >= `PipelineProcess.started_at`. For resolver, this is `resolver_updated_at >= started_at`. For crawler, `crawled_orgs.last_crawled_at >= started_at`. For classifier, `reports` rows classified since `started_at`.

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

**Results tables** (two):
- **Recently crawled orgs**: org, candidate count, fetched count, confirmed report count, last-crawled timestamp.
- **Recently archived reports**: filename (extracted from URL basename), org, file size, source URL, classification (if available), archived timestamp. Filename is the human-readable identifier — SHAs are kept for click-through detail only.

### 4. Classifier Controls (`/classifier/`)

**Status panel**: Current process state, PID, uptime, reports classified this session.

**Control form**:
- Limit (integer, 0 = no limit)
- Max workers (integer, 1-32)
- Re-classify (checkbox — re-classify rows that already have a classification)
- Start / Stop buttons

**Results table**: Recent classifications with filename (from URL basename), org, classification, confidence, classified timestamp. SHA kept internally for links but not displayed as a column — filename is the human-readable identifier.

### 5. Org Browser (`/orgs/`)

Paginated table of all nonprofits with:
- Filters: state, resolver_status, resolver_method
- Columns: EIN, name, city, state, URL, status, confidence, method, timestamp
- Click-through to detail view showing full resolver_reason and candidates

### 6. Reports Browser (`/reports/`)

Paginated table of all classified reports, parallel in shape to the Org Browser. Separates "browsing the output" from the Classifier page's "running the classifier" concern.

- Filters: org (EIN or name search), classification, report_year, archived_at date range
- Columns: filename (URL basename), org, classification, confidence, year, size, archived_at
- Click-through to detail view showing: full source URL, SHA, first_page_text, PDF metadata, S3 key. Download link via signed S3 URL (see Iteration 2 notes).

## Database Configuration

Django connects to the existing RDS instance using the same SSM-sourced credentials as the pipeline. Credentials are loaded via `lavandula.common.secrets` (the existing SSM helper) to stay consistent with other pipeline tools.

```python
# settings.py
from lavandula.common.secrets import get_secret

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': get_secret('rds-database'),
        'USER': get_secret('rds-app-user'),
        'HOST': get_secret('rds-endpoint'),
        'PORT': get_secret('rds-port'),
        'OPTIONS': {
            'options': '-c search_path=lava_impact,public',
        },
    }
}
```

**Schema separation**: Django's managed tables (`pipeline_processes`, `pipeline_audit_log`, `django_migrations`, `auth_user`, etc.) live in a separate `lava_dashboard` schema, not `lava_impact`. This isolates Django's DDL privileges from production data. The DB user for the dashboard has:
- `CREATE`/`ALTER`/`DROP` on `lava_dashboard` (for Django migrations)
- `SELECT` only on `lava_impact` (read-only access to pipeline data)

Settings use a database router to direct managed models to `lava_dashboard` and unmanaged models to `lava_impact`:

```python
DATABASES = {
    'default': {  # lava_dashboard schema — Django managed tables
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': get_secret('rds-database'),
        'USER': get_secret('rds-dashboard-user'),  # limited privileges
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

A custom database router directs reads for unmanaged models to the `pipeline` DB alias.

## Security

- **Network binding**: Binds to `127.0.0.1:8000` only, accessed via SSH tunnel
- **Authentication**: Django's built-in `AuthenticationMiddleware` + `LoginRequiredMixin` on all views. A single superuser created via `manage.py createsuperuser` provides defense-in-depth beyond the SSH tunnel. This prevents other local processes/users from accessing the dashboard
- **DEBUG = False**: Hardcoded in production settings. Prevents Django's debug pages from leaking DB credentials and environment variables on errors
- **Process execution**: Only predefined CLI commands via `COMMAND_MAP` allowlist, no arbitrary shell execution. Commands are built as `argv` arrays (never shell strings) to prevent injection. User-supplied text parameters (archive destination, etc.) are validated against strict regexes before use
- **No secrets via CLI flags**: Pipeline tools load their own credentials from SSM/environment. The dashboard never passes secrets as command-line arguments (which would be visible via `ps aux`)
- **CSRF protection**: Django's built-in CSRF middleware. HTMX configured to include CSRF token in request headers via `hx-headers` on `<body>` tag
- **Input validation**: All form parameters validated server-side against the `COMMAND_MAP` type/range/pattern definitions. Choice fields use allowlists. Numeric fields have min/max bounds. Text fields validated against whitelisted patterns (e.g., S3 bucket URIs match `^s3://[a-z0-9]...`)
- **Log viewing safety**: Log file paths are stored in the `PipelineProcess` model at process creation time. The view only accepts a `process_id`, looks up the stored path, and verifies it resolves (via `os.path.realpath`) to within the `lavandula/logs/dashboard/` directory before reading
- **Audit logging**: All start/stop actions are logged to a Django-managed `PipelineAuditLog` table recording the action, parameters, timestamp, and source IP. This provides an incident response trail even with a single operator
- **Process limits**: Maximum 3 concurrent subprocesses (one per pipeline stage). The dashboard refuses to start a process if the global count is already at the limit

## Acceptance Criteria

### Dashboard
- AC01: Overview page loads showing seed/resolver/crawler/classifier stats
- AC02: Stats auto-refresh via HTMX every 5 seconds without full page reload
- AC03: Resolver status breakdown shows counts by `resolver_method` (all methods present in DB: haiku, gemma, codex-gpt54-v1, codex-gpt54mini-v1, gemini-flash-v1, claude-opus-v1, etc.)
- AC04: Reports breakdown shows counts by classification and year

### Process Controls
- AC05: Resolver can be started from the UI with model selection and CLI arguments per Command Mapping
- AC06: Crawler can be started from the UI with archive destination, limit, and max-workers
- AC07: Classifier can be started from the UI with limit and max-workers
- AC08: Running processes can be stopped from the UI (SIGTERM → 10s wait → SIGKILL)
- AC09: Process status shows running/stopped/error with PID and uptime
- AC10: Starting a process that's already running shows an error, doesn't spawn a duplicate (enforced via `select_for_update`)
- AC20: Each process detail page shows last 100 lines of the current log file

### Org Browser
- AC11: Orgs table paginates at 50 rows per page
- AC12: Orgs filterable by state, resolver_status, resolver_method
- AC13: Org detail view shows full resolver_reason and website_candidates_json

### Reports Browser
- AC24: Reports table paginates at 50 rows per page
- AC25: Reports filterable by org, classification, report_year, archived_at range
- AC26: Reports table displays filename (URL basename) as the primary human-readable identifier
- AC27: Report detail view exposes a "Download" action that returns a signed S3 URL valid for 5 minutes
- AC28: Crawler and Classifier results tables display filename in place of raw SHA256

### Integration
- AC14: Django reads from existing lava_impact tables without running migrations against them
- AC15: Process manager correctly detects process death (PID no longer running or `/proc/{pid}/cmdline` mismatch)
- AC16: Dashboard works when no processes are running (all stopped state)
- AC21: Dashboard startup cleans up stale `running` rows from previous dashboard sessions

### Infrastructure
- AC17: `python manage.py runserver 127.0.0.1:8000` starts the dashboard
- AC18: Django project lives at `lavandula/dashboard/`
- AC19: No additional system dependencies beyond `pip install django psycopg2-binary` (HTMX, Tailwind, Chart.js loaded from CDN)
- AC22: All start/stop actions logged to `PipelineAuditLog` with timestamp, parameters, and source IP
- AC23: Login required to access any dashboard page

## Testing Strategy

### Unit Tests
- **Command builder**: Verify `COMMAND_MAP` produces correct `argv` arrays for all parameter combinations. Verify invalid parameters are rejected (out-of-range, bad patterns, injection attempts)
- **Process state machine**: Test state transitions (stopped→running, running→stopped, running→error). Test stale PID cleanup on startup
- **PID verification**: Mock `/proc/{pid}/cmdline` reads. Test PID reuse detection (cmdline mismatch)
- **Log tail**: Test reading last N lines from log files, including empty/missing/still-being-written files

### View Tests
- **Dashboard views**: Use Django's test client to verify each page renders, returns correct HTTP status, and includes expected template context
- **Form validation**: Submit forms with valid/invalid data, verify server-side validation matches `COMMAND_MAP` constraints
- **HTMX fragments**: Verify polling endpoints return HTML fragments (not full pages)

### Integration Tests
- **Unmanaged model smoke tests**: Verify Django can read from `nonprofits_seed`, `reports`, `crawled_orgs` without errors. Use a test database with the lava_impact schema
- **Process lifecycle**: Start a dummy long-running process, verify PID tracking, stop it, verify cleanup (use a simple `sleep` command as the test process)

## Traps to Avoid

1. **Don't run Django migrations on lava_impact tables** — use `managed = False` for all existing tables. Only `pipeline_processes` (and Django metadata) are Django-managed.
2. **Don't store secrets in settings.py** — load from SSM via `lavandula.common.secrets`, same as other pipeline tools.
3. **Don't build a SPA** — server-rendered templates + HTMX keeps it simple and fast.
4. **Don't implement websockets in Iteration 1** — HTMX polling is sufficient. Django Channels is an Iteration 2+ addition.
5. **Don't over-engineer process management** — subprocess + PID is adequate for single-host. Celery/supervisord is Iteration 2+ if we go multi-host.
6. **Don't duplicate the `search_path` schema** — Django's `OPTIONS` sets `search_path` at connection time so all queries hit `lava_impact`.
7. **Don't build CLI commands as shell strings** — always construct `argv` arrays to prevent command injection.
8. **Don't forget CSRF + HTMX** — configure `hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'` on the body tag so HTMX POST requests include the Django CSRF token.

## Consultation Log

### Round 1 — Spec Review (2026-04-23)

**Gemini (APPROVE, HIGH confidence)**:
- Add log tail viewing for ops visibility → **Added**: last 100 lines in detail views
- PID identity verification via `/proc/[pid]/cmdline` → **Added**: to process manager
- Django metadata tables will pollute lava_impact → **Addressed**: documented as acceptable for single-operator
- Concurrency/race conditions on double-start → **Added**: `select_for_update` locking
- Environment/PYTHONPATH for subprocess spawning → **Added**: explicit cwd/PYTHONPATH in process manager
- Graceful → forceful shutdown → **Added**: SIGTERM → 10s wait → SIGKILL
- Minor: log directory, SSM via secrets.py, CSRF+HTMX → **All addressed**

**Codex (REQUEST_CHANGES, HIGH confidence)**:
- Process lifecycle underspecified → **Added**: full state machine, stale PID cleanup, command verification
- Command mapping missing → **Added**: complete `COMMAND_MAP` with types, ranges, patterns
- Managed table strategy incomplete → **Revised**: separate `lava_dashboard` schema (see Round 2)
- Missing `website_candidates_json` in model → **Fixed**: added to `NonprofitSeed`
- Session metrics ambiguous → **Clarified**: DB deltas since `started_at`
- Security too thin → **Expanded**: input validation, argv construction, regex patterns
- Failure/concurrency cases → **Added**: select_for_update, stale cleanup, SIGKILL fallback
- AC contradictions (methods, bind address) → **Fixed**: AC03 shows all methods from DB, AC17 uses 127.0.0.1
- Testing strategy missing → **Added**: full testing strategy section
- Classifier controls wrong (Gemma vs Anthropic) → **Fixed**: matches actual classify_null CLI

### Round 2 — Red Team Security Review (2026-04-23)

**Gemini Red Team (REQUEST_CHANGES)**:

**CRITICAL**:
1. Zero authentication policy → **Fixed**: added Django AuthenticationMiddleware + LoginRequiredMixin + createsuperuser
2. DB privilege over-extension / schema pollution → **Fixed**: separate `lava_dashboard` schema for Django managed tables, read-only access to `lava_impact`

**HIGH**:
3. Path traversal in log viewing → **Fixed**: log paths stored in DB at creation, validated via `os.path.realpath` against allowed directory
4. Lack of audit logging → **Fixed**: added `PipelineAuditLog` model for all start/stop actions

**MEDIUM**:
5. Sensitive data in debug pages → **Fixed**: `DEBUG = False` hardcoded
6. Resource exhaustion (fork bomb) → **Fixed**: max 3 concurrent subprocesses (one per pipeline stage)

**LOW**:
7. S3 regex strictness → already mitigated by argv construction (no shell interpretation)
8. Cleartext secrets in process list → **Fixed**: documented that pipeline tools load own credentials, dashboard never passes secrets as CLI flags

## Future Iterations

### Iteration 2: Report Data Viewer
- PDF page viewer with extracted text overlay
- Structure/styling extraction display
- Training data annotation interface
- Depends on 0014 (PDF extraction)
- **PDF byte access**: to be resolved in Iteration 2 spec — two candidate approaches: (a) generate short-lived signed S3 URLs per-request and redirect the client, (b) proxy through a Django view that streams bytes. Iteration 1 does not serve PDF bytes; the Reports Browser detail view uses the signed-URL approach as its "Download" action.

### Iteration 3: Report Interviewer MVP
- Structured form wizard (Django forms)
- Section-by-section question flow
- Template styling from extracted reports
- Text input initially, speech-to-text layered in
- AI-assisted suggestions as training data grows
- **User model**: Iteration 1 uses a single superuser. Iteration 3 will have multiple users (different interviewers, report authors). Django's default `auth_user` model supports this trivially — no schema migration required at Iteration 1, just adding users via admin later.

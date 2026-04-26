# Plan 0019: Pipeline Orchestrator & Control Center

**Spec**: `locard/specs/0019-pipeline-dashboard.md`
**Date**: 2026-04-26

## Overview

Build a Django web app at `lavandula/dashboard/` that provides a job orchestrator (state machine + runner), real-time pipeline visibility, ad-hoc process controls, and org/reports browsers — all backed by the existing RDS instance.

**9 phases**, ordered so each builds on the last. Phases 1-3 are the structural foundation; Phases 4-7 are the feature surfaces; Phase 8 is security hardening; Phase 9 is integration testing.

---

## Phase 1: Django Project Scaffolding

**Goal**: Bootable Django project with dual-database configuration and database router.

**Files created**:
```
lavandula/dashboard/
  manage.py
  dashboard/
    __init__.py
    settings.py
    urls.py
    wsgi.py
  pipeline/
    __init__.py
    apps.py
    routers.py
```

**Steps**:
1. `django-admin startproject dashboard lavandula/dashboard/`
2. `cd lavandula/dashboard && python manage.py startapp pipeline`
3. Configure `settings.py`:
   - Import `get_secret` from `lavandula.common.secrets` (add parent dirs to `sys.path`)
   - Two database aliases: `default` (lava_dashboard schema, dashboard-user) and `pipeline` (lava_impact schema, app-user read-only)
   - `DATABASE_ROUTERS = ['pipeline.routers.PipelineRouter']`
   - `DEBUG = False`
   - `ALLOWED_HOSTS = ['127.0.0.1', 'localhost']`
   - Session settings: `SESSION_COOKIE_AGE = 3600`, `SESSION_COOKIE_HTTPONLY = True`, `SESSION_COOKIE_SECURE = True`
   - `INSTALLED_APPS`: add `pipeline`, `django.contrib.auth`, `django.contrib.sessions`, `django.contrib.contenttypes`
   - Static files: `STATIC_URL = '/static/'`, `STATICFILES_DIRS = [pipeline/static/]`
4. Write `pipeline/routers.py` — `PipelineRouter` class per spec: routes unmanaged model reads to `pipeline` alias, raises `RuntimeError` on writes to unmanaged models, blocks migrations on `pipeline` alias
5. Create `requirements.txt`: `django>=5.0,<6.0`, `psycopg2-binary`

**Infrastructure prerequisite** (manual, before Phase 1 runs):
- Create PostgreSQL role `lava_dashboard_user` with `CREATE` on `lava_dashboard` schema
- Grant `USAGE` on `lava_impact` schema + `SELECT` on all `lava_impact` tables to this role
- Store credentials in SSM as `rds-dashboard-user`
- Create `lava_dashboard` schema: `CREATE SCHEMA IF NOT EXISTS lava_dashboard`

**Verify**: `python manage.py check` passes. `python manage.py migrate --run-syncdb` creates Django system tables in `lava_dashboard`.

**ACs**: AC30, AC32, AC33 (partial)

---

## Phase 2: Models

**Goal**: All Django models defined — unmanaged pipeline models and managed job/process/audit models.

**Files created/modified**:
```
pipeline/models.py
```

**Steps**:
1. Unmanaged models (`managed = False`):
   - `NonprofitSeed` — maps to `nonprofits_seed` (PK: `ein`)
   - `Report` — maps to `reports` (PK: `content_sha256`)
   - `CrawledOrg` — maps to `crawled_orgs` (PK: `ein`)
2. Managed models:
   - `Job` — per spec: `state_code` (nullable), `phase` (choices: seed/resolve/crawl/classify), `status` (choices: pending/running/completed/failed/cancelled), `host`, `pid`, `config_json`, timestamps, `exit_code`, `error_message`, `progress_current`, `progress_total`, `depends_on` (self FK), `last_heartbeat`. Indexes on `(status, phase)` and `(state_code, phase)`. Table: `jobs`
   - `PipelineProcess` — `name` (unique), `pid`, `status`, `started_at`, `config_json`, `last_heartbeat`, `log_file`. Table: `pipeline_processes`
   - `PipelineAuditLog` — `action`, `process_name`, `parameters` (JSON), `source_ip`, `timestamp`. Table: `pipeline_audit_log`
3. `python manage.py makemigrations pipeline`
4. `python manage.py migrate`

**Verify**: All three managed tables exist in `lava_dashboard`. Unmanaged models can read from `lava_impact` via Django ORM shell: `NonprofitSeed.objects.count()`.

**ACs**: AC25

---

## Phase 3: Job Orchestrator Core

**Goal**: State machine logic and the `run_orchestrator` management command.

**Files created**:
```
pipeline/orchestrator.py
pipeline/management/__init__.py
pipeline/management/commands/__init__.py
pipeline/management/commands/run_orchestrator.py
```

**Steps**:

### 3a: orchestrator.py — State Machine + Command Builder

1. `COMMAND_MAP` dict — per spec, keys: `seed`, `resolve`, `crawl`, `classify`. Each entry has `cmd` (argv prefix) and `params` (name → type/min/max/pattern/flag). Validation function `build_argv(phase, config_json) -> list[str]` that rejects unknown keys and validates each param against its type def
2. `create_state_jobs(state_code, phases, config_overrides, host) -> list[Job]`:
   - Check duplicate policy: reject if pending/running job exists for same `state_code + phase`
   - Create seed job (pending), then resolve job (pending, depends_on=seed)
   - Return created jobs
3. `create_crawl_job(config_overrides, host) -> Job`:
   - Duplicate check for phase=crawl, status in (pending, running)
   - Create with `state_code=NULL`, no depends_on
4. `retry_job(job) -> Job`:
   - Create new pending job with same phase/state_code/config
   - Rewire dependents: any job whose `depends_on` points to the failed job gets pointed to the new job
5. `cancel_job(job)`:
   - If running: send SIGTERM, wait 10s, SIGKILL. Use `os.killpg` for process group
   - Mark cancelled
   - Cascade: cancel all downstream dependents recursively
6. `get_eligible_jobs(host) -> QuerySet`:
   - status=pending, host matches, and (depends_on is NULL OR depends_on.status=completed)
   - Exclude phases that have a running job or running ad-hoc process
7. `check_phase_conflict(phase) -> bool`:
   - True if a Job with status=running exists for this phase OR a PipelineProcess with status=running exists for this phase name

### 3b: run_orchestrator management command

1. On startup: scan for `running` jobs where `host=socket.gethostname()`. For each, check PID liveness via `os.kill(pid, 0)`. Dead PIDs → mark failed with `error_message="orphaned: PID not found on restart"`
2. Main loop (every 10s):
   - For each running job owned by this host: poll PID, update `last_heartbeat`. If exited, update status (completed if exit 0, failed otherwise), record `exit_code`
   - Query eligible jobs, pick oldest
   - Build argv via `build_argv`, spawn via `subprocess.Popen` with `os.setpgrp` as `preexec_fn`, stdout/stderr to log file
   - Record PID, `started_at`, `log_file` on the job
   - Handle `Popen` OSError/FileNotFoundError: mark job failed immediately
3. Signal handling: SIGTERM/SIGINT → set shutdown flag, finish current poll, exit cleanly

**Verify**: Unit tests for state machine (create, retry with rewiring, cancel with cascade, eligibility filtering, duplicate rejection). Management command starts and exits on SIGTERM.

**ACs**: AC01, AC02, AC03, AC04, AC05, AC06, AC07

---

## Phase 4: Process Manager

**Goal**: Ad-hoc process start/stop for one-off pipeline runs outside the job queue.

**Files created/modified**:
```
pipeline/process_manager.py
```

**Steps**:
1. `start_process(name, config_json) -> PipelineProcess`:
   - Acquire `select_for_update` lock on PipelineProcess row (get_or_create by name)
   - Check phase conflict with `check_phase_conflict(name)` — reject if job running for same phase
   - Check PipelineProcess.status != running (or if running, verify PID is actually alive + cmdline matches)
   - Build argv via `build_argv(name, config_json)`
   - Spawn via `subprocess.Popen`, `os.setpgrp`, stdout/stderr to `lavandula/logs/dashboard/{name}_{timestamp}.log`
   - Record PID, started_at, log_file, status=running
   - Handle OSError: mark failed, PID=null
2. `stop_process(name)`:
   - Send SIGTERM to process group via `os.killpg`
   - Wait up to 10s, then SIGKILL
   - Update status=stopped
3. `check_process(name) -> status`:
   - Verify PID alive AND `/proc/{pid}/cmdline` matches
   - If dead or mismatch: update status=stopped
   - Update `last_heartbeat` if alive
4. `cleanup_stale()`:
   - On startup: scan all `running` rows, validate PID + cmdline, mark dead as stopped
5. `read_log_tail(log_file, n_lines=100) -> str`:
   - Validate `os.path.realpath(log_file)` is under allowed log directory
   - Read last N lines

**Verify**: Unit tests for start/stop lifecycle, stale cleanup, log tail path validation.

**ACs**: AC13, AC14, AC15, AC16, AC17, AC26, AC28

---

## Phase 5: Templates & Base UI

**Goal**: Base template with nav, Tailwind CSS, HTMX, CSRF configuration. Overview dashboard.

**Files created**:
```
pipeline/templates/pipeline/
  base.html
  login.html
  dashboard.html
  partials/
    dashboard_stats.html
pipeline/static/pipeline/
  css/custom.css (minimal overrides if needed)
  js/  (empty, Chart.js via CDN)
pipeline/templatetags/__init__.py
pipeline/templatetags/pipeline_tags.py
pipeline/views.py
pipeline/forms.py
dashboard/urls.py (update)
```

**Steps**:
1. `base.html`:
   - Tailwind CSS via CDN `<script src="https://cdn.tailwindcss.com">`
   - HTMX via CDN `<script src="https://unpkg.com/htmx.org@2.0.0">`
   - Chart.js via CDN
   - `<body hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'>`
   - Navigation sidebar: Dashboard, Jobs, Resolver, Crawler, Classifier, Orgs, Reports
   - `{% block content %}` for page content
   - Logout link
2. `login.html` — simple Django auth form
3. `dashboard.html`:
   - Job Queue summary: active/pending/completed counts
   - Seed Pool: total orgs by state (top 10 states), by resolver_status
   - Resolver: resolved/unresolved/ambiguous counts
   - Crawler: crawled orgs, total reports
   - Classifier: classified/unclassified counts, by classification type
   - Each section wrapped in HTMX-pollable div (`hx-get`, `hx-trigger="every 5s"`)
4. `partials/dashboard_stats.html` — HTMX fragment for stats refresh
5. `views.py`:
   - `DashboardView(LoginRequiredMixin, TemplateView)` — aggregate queries across unmanaged models + Job model
   - `DashboardStatsPartial` — returns stats fragment for HTMX
   - `LoginView` — Django's built-in `auth_views.LoginView`
6. `pipeline_tags.py` — template filters: `duration` (timedelta → human-readable), `percentage` (current/total → percent string)
7. URL configuration: `/` → dashboard, `/login/` → login, `/logout/` → logout

**Verify**: `runserver` starts, login page renders, dashboard shows real pipeline stats from RDS.

**ACs**: AC09, AC10, AC34

---

## Phase 6: Job Queue & Detail Views

**Goal**: Job queue page, job detail page, "Run State" form.

**Files created/modified**:
```
pipeline/templates/pipeline/
  jobs.html
  job_detail.html
  partials/
    job_list.html
    job_progress.html
pipeline/forms.py (update)
pipeline/views.py (update)
dashboard/urls.py (update)
```

**Steps**:
1. `forms.py`:
   - `RunStateForm`: multi-select state dropdown (all 50 + territories), phase checkboxes (seed, resolve — checked by default), expandable config panel (LLM model, brave QPS, consumer threads, etc.)
   - `RunCrawlForm`: archive dest, limit, concurrency settings
   - All form fields validated server-side against `COMMAND_MAP` type/range/pattern defs
2. `jobs.html`:
   - Active jobs section: running jobs with progress bars, phase badge, state, host, elapsed time
   - Pending jobs section: queued with dependency chain display
   - "Run State" form (POST, creates chained jobs)
   - Job history: completed/failed/cancelled with duration, exit code, log link
   - HTMX polling on active + pending sections
3. `job_detail.html`:
   - Status, phase, state, host, PID, timestamps
   - Progress bar (or indeterminate indicator if `progress_total` is NULL)
   - Configuration JSON display
   - Last 100 lines of log (HTMX-refreshed every 5s via `read_log_tail`)
   - Cancel button (if pending/running) → POST to cancel endpoint
   - Retry button (if failed) → POST to retry endpoint
4. `views.py`:
   - `JobListView(LoginRequiredMixin, ListView)` — all jobs, ordered by created_at desc
   - `JobDetailView(LoginRequiredMixin, DetailView)` — single job
   - `JobCreateView(LoginRequiredMixin, FormView)` — processes RunStateForm, calls `create_state_jobs`
   - `CrawlJobCreateView(LoginRequiredMixin, FormView)` — processes RunCrawlForm
   - `JobCancelView(LoginRequiredMixin, View)` — POST-only, calls `cancel_job`
   - `JobRetryView(LoginRequiredMixin, View)` — POST-only, calls `retry_job`
   - `JobProgressPartial` — HTMX fragment for progress updates
   - `JobLogPartial` — HTMX fragment for log tail
   - All mutation views log to `PipelineAuditLog`
5. Progress tracking per spec:
   - Seed: query `NonprofitSeed.objects.filter(state=job.state_code).count()` — display as "X orgs found"
   - Resolve: count where `resolver_updated_at >= job.started_at` and `state=job.state_code`
   - Crawl: count `CrawledOrg` rows
   - Classify: count `Report` rows where classification is not null

**Verify**: Create chained jobs via UI, verify dependency display, cancel/retry work, progress updates in real-time.

**ACs**: AC01, AC04, AC05, AC06, AC08

---

## Phase 7: Pipeline Controls + Browsers

**Goal**: Resolver/Crawler/Classifier control pages, Org browser, Reports browser.

**Files created/modified**:
```
pipeline/templates/pipeline/
  resolver.html
  crawler.html
  classifier.html
  orgs.html
  org_detail.html
  reports.html
  report_detail.html
  partials/
    resolver_results.html
    crawler_results.html
    classifier_results.html
    org_table.html
    report_table.html
pipeline/forms.py (update)
pipeline/views.py (update)
dashboard/urls.py (update)
```

**Steps**:

### 7a: Pipeline Control Pages

1. Forms: `ResolverForm`, `CrawlerForm`, `ClassifierForm` — each mirrors the `COMMAND_MAP` params for that phase. Start/Stop buttons.
2. Each control page:
   - Status panel: current job or ad-hoc process state, PID, uptime, progress
   - Ad-hoc form for one-off runs
   - Results table (recent items, HTMX-refreshed)
3. Views:
   - `ResolverView` — shows status + form + recent resolver results (last 50 `NonprofitSeed` by `resolver_updated_at` desc)
   - `CrawlerView` — status + form + recent `CrawledOrg` rows + recent `Report` rows
   - `ClassifierView` — status + form + recent classified reports
   - `ProcessStartView(phase)` — POST: calls `start_process`, logs audit
   - `ProcessStopView(phase)` — POST: calls `stop_process`, logs audit

### 7b: Org Browser

1. `orgs.html` — paginated table (50/page) with filters: state (dropdown), resolver_status, resolver_method
2. `org_detail.html` — full detail: EIN, name, city, state, URL, status, confidence, method, reason, website_candidates_json (formatted JSON)
3. Views: `OrgListView(LoginRequiredMixin, ListView)` with `get_queryset` applying filters, `OrgDetailView`

### 7c: Reports Browser

1. `reports.html` — paginated table (50/page) with filters: org (EIN/name search), classification, report_year, archived_at range
2. `report_detail.html` — full detail: SHA, source URL (redacted), classification, confidence, year, size, first_page_text, page_count. S3 download link.
3. Views: `ReportListView`, `ReportDetailView`
4. S3 download view: accepts `content_sha256`, validates via ORM lookup, generates presigned S3 URL (`pdfs/{sha}.pdf`, 300s expiry), returns redirect

**Verify**: All control pages render, ad-hoc start/stop works, org/report browsers paginate and filter correctly, S3 download link works.

**ACs**: AC11, AC12, AC13, AC14, AC15, AC16, AC17, AC18, AC19, AC20, AC21, AC22, AC23, AC24

---

## Phase 8: Security Hardening

**Goal**: Ensure all security requirements from spec + red team reviews.

**Steps**:
1. Verify `LoginRequiredMixin` on every view (grep for views without it)
2. Verify `DEBUG = False` is hardcoded (not from env)
3. Verify session settings: `SESSION_COOKIE_AGE`, `SESSION_COOKIE_HTTPONLY`, `SESSION_COOKIE_SECURE`, `LOGOUT_REDIRECT_URL`
4. Verify CSRF: HTMX body tag has `hx-headers` with CSRF token. All POST views have CSRF middleware
5. Verify command builder: no shell=True anywhere, all commands built as argv arrays
6. Verify log path validation: `os.path.realpath` check in `read_log_tail`
7. Verify no `|safe` filter on log content or error messages in templates (grep templates)
8. Verify audit logging: all mutation endpoints (job create/cancel/retry, process start/stop) write to `PipelineAuditLog`
9. Create `manage.py createsuperuser` documentation in README within `lavandula/dashboard/`
10. Verify PipelineRouter blocks writes to unmanaged models

**Verify**: Security checklist passes. Attempt to access views without login → redirect. Attempt shell injection via form params → rejected.

**ACs**: AC29, AC34

---

## Phase 9: Integration Testing

**Goal**: Full test suite covering the spec's testing requirements.

**Files created**:
```
pipeline/tests/
  __init__.py
  test_models.py
  test_orchestrator.py
  test_process_manager.py
  test_views.py
  test_router.py
  test_command_builder.py
  test_integration.py
```

**Steps**:

### Unit Tests
1. `test_models.py` — model creation, field constraints, indexes
2. `test_orchestrator.py`:
   - Job creation with dependencies
   - State machine transitions (pending→running→completed, pending→running→failed)
   - Eligibility logic (dependencies, concurrency limits)
   - Duplicate rejection
   - Retry rewires dependents
   - Cancel cascades to downstream
3. `test_command_builder.py`:
   - Valid params produce correct argv
   - Invalid params (wrong type, out of range, bad pattern) rejected
   - Unknown keys rejected
4. `test_process_manager.py`:
   - Start/stop lifecycle
   - Stale PID cleanup
   - PID reuse detection (cmdline mismatch)
   - Log tail path traversal prevention
   - Phase conflict detection (job + ad-hoc mutual exclusion)
5. `test_router.py`:
   - Unmanaged models routed to `pipeline` alias for reads
   - Writes to unmanaged models raise RuntimeError
   - Migrations blocked on `pipeline` alias

### View Tests
6. `test_views.py`:
   - All views require authentication (unauthenticated → 302 to login)
   - Dashboard renders with correct context
   - Job creation form validation
   - Cancel/retry endpoints work
   - HTMX partials return fragments (no full page)
   - Audit log entries created on mutations

### Integration Tests
7. `test_integration.py`:
   - Unmanaged models can query real `lava_impact` tables
   - Job lifecycle: create → runner picks → (mock) subprocess completes → status updates
   - Dependency chain: seed completes → resolve becomes eligible → completes → no automatic crawl
   - Retry rewiring end-to-end
   - Cancel cascade end-to-end
   - Concurrent duplicate submission → second rejected
   - Crawl job independence (state_code=NULL, no depends_on, immediately eligible)

**Verify**: `python manage.py test pipeline` passes. Coverage on orchestrator, process manager, and command builder ≥80%.

**ACs**: All ACs verified via tests

---

## Infrastructure Setup (Pre-Implementation)

These are manual steps the operator performs before the builder starts:

1. **Create PostgreSQL schema and role**:
   ```sql
   CREATE SCHEMA IF NOT EXISTS lava_dashboard;
   CREATE ROLE lava_dashboard_user WITH LOGIN PASSWORD '...';
   GRANT USAGE ON SCHEMA lava_dashboard TO lava_dashboard_user;
   GRANT ALL ON SCHEMA lava_dashboard TO lava_dashboard_user;
   GRANT USAGE ON SCHEMA lava_impact TO lava_dashboard_user;
   GRANT SELECT ON ALL TABLES IN SCHEMA lava_impact TO lava_dashboard_user;
   ALTER DEFAULT PRIVILEGES IN SCHEMA lava_impact GRANT SELECT ON TABLES TO lava_dashboard_user;
   ```
2. **Store credentials in SSM**: `rds-dashboard-user` → `lava_dashboard_user`, `rds-dashboard-password` → password
3. **Install dependencies on the dashboard host**: `pip install django psycopg2-binary`

---

## Acceptance Criteria Mapping

| AC | Phase | How Verified |
|----|-------|-------------|
| AC01 | 3, 6 | RunStateForm creates seed→resolve chain; crawl separate |
| AC02 | 3 | run_orchestrator picks + executes eligible jobs |
| AC03 | 3 | Depends_on blocks until dependency completed |
| AC04 | 3, 6 | Retry button creates new pending, rewires dependents |
| AC05 | 3, 6 | Cancel sends SIGTERM→SIGKILL, cascades |
| AC06 | 6 | Multi-state form creates jobs for all states |
| AC07 | 3 | Concurrency limit: 1 running per phase |
| AC08 | 6 | HTMX progress polling on job detail |
| AC09 | 5 | Overview dashboard with all sections |
| AC10 | 5 | HTMX 5s polling, no full reload |
| AC11 | 7 | Resolver status by method |
| AC12 | 7 | Reports by classification + year |
| AC13 | 4, 7 | Ad-hoc start from UI |
| AC14 | 4, 7 | Stop from UI |
| AC15 | 4, 7 | Running/stopped/error + PID + uptime |
| AC16 | 4 | select_for_update prevents double-start |
| AC17 | 4, 6 | Log tail on detail pages |
| AC18 | 7 | 50-row pagination on orgs |
| AC19 | 7 | State/status/method filters |
| AC20 | 7 | Org detail with reason + candidates |
| AC21 | 7 | 50-row pagination on reports |
| AC22 | 7 | Org/classification/year/date filters |
| AC23 | 7 | URL basename as filename |
| AC24 | 7 | Presigned S3 URL (5-min) |
| AC25 | 2 | Unmanaged models, no migrations on lava_impact |
| AC26 | 4 | PID gone + cmdline mismatch detection |
| AC27 | 5, 9 | Dashboard renders with no running processes |
| AC28 | 4 | Startup cleans stale running rows |
| AC29 | 8 | All mutations logged to PipelineAuditLog |
| AC30 | 1 | runserver 127.0.0.1:8000 works |
| AC31 | 3 | run_orchestrator management command |
| AC32 | 1 | Lives at lavandula/dashboard/ |
| AC33 | 1 | django + psycopg2-binary only |
| AC34 | 5, 8 | LoginRequiredMixin on all views |

---

## Estimated Effort

| Phase | Description | Relative Size |
|-------|-------------|--------------|
| 1 | Django scaffolding | Small |
| 2 | Models | Small |
| 3 | Orchestrator core | Large |
| 4 | Process manager | Medium |
| 5 | Templates & base UI | Medium |
| 6 | Job queue views | Large |
| 7 | Controls + browsers | Large |
| 8 | Security hardening | Small |
| 9 | Integration testing | Large |

## Consultation Log

(Pending — will be filled after reviews)

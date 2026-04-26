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
   - `Job` — per spec: `state_code` (nullable), `phase` (choices: seed/resolve/crawl/classify), `status` (choices: pending/running/completed/failed/cancelled), `host`, `pid`, `config_json`, timestamps, `exit_code`, `error_message`, `progress_current`, `progress_total`, `depends_on` (self FK), `last_heartbeat`. Indexes on `(status, phase)` and `(state_code, phase)`. **Two partial unique indexes** for duplicate job rejection: (1) on `(state_code, phase)` filtered to `state_code IS NOT NULL AND status IN ('pending', 'running')` — prevents duplicate per-state jobs; (2) on `(phase,)` filtered to `state_code IS NULL AND status IN ('pending', 'running')` — prevents duplicate global jobs (crawl/classify) since PostgreSQL NULL values don't collide in unique indexes. Table: `jobs`
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
   - `phases` is a list from the form checkboxes. **Allowed combinations**: `['seed']` (seed only — useful for re-seeding without re-resolving), `['seed', 'resolve']` (full pipeline — default), `['resolve']` (resolve only — useful when seed data already exists). If `resolve` is selected without `seed`, no dependency is created (resolve job is immediately eligible). If both are selected, resolve `depends_on` seed. Other combinations are rejected at the form validation layer.
   - **Race-safe duplicate check**: wrap in `transaction.atomic()` with `select_for_update` on existing pending/running jobs for the same `state_code + phase`. The partial unique indexes on the `jobs` table serve as a DB-level backstop against races that slip past the application check
   - Create jobs per selected phases with correct dependency wiring
   - Return created jobs
3. `create_crawl_job(config_overrides, host) -> Job`:
   - Race-safe duplicate check: same `transaction.atomic()` + `select_for_update` pattern for phase=crawl, status in (pending, running)
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
   - For each running job owned by this host: poll PID. If alive, write `last_heartbeat = now()` (ensures heartbeat is updated every ~10s, well within the 30s spec requirement; the spec's 30s is the *maximum* interval, not the target). If exited, update status (completed if exit 0, failed otherwise), record `exit_code`. **DB-update-failure handling**: if the status write fails, retry once after 1s. If the retry also fails, log to stderr and kill the subprocess via `os.killpg` to prevent an untracked orphan.
   - Query eligible jobs, pick oldest
   - Build argv via `build_argv`, spawn via `subprocess.Popen` with `preexec_fn=os.setpgrp`, `cwd=PROJECT_ROOT` (the repo root, e.g. `/home/ubuntu/research`), and `env` dict that copies `os.environ` plus sets `PYTHONPATH=PROJECT_ROOT`. stdout/stderr redirected to `lavandula/logs/dashboard/{phase}_{state}_{timestamp}.log`
   - Record PID, `started_at`, `log_file` on the job
   - Handle `Popen` OSError/FileNotFoundError: mark job failed immediately with exception message in `error_message`
   - Handle immediate exit (< 1 poll cycle): detected on next poll, marked `failed` or `completed` per exit code
3. Signal handling: SIGTERM/SIGINT → set shutdown flag, finish current poll, exit cleanly
4. Constants: `PROJECT_ROOT = Path(__file__).resolve().parents[3]` (three levels up from `pipeline/management/commands/`), `LOG_DIR = PROJECT_ROOT / "lavandula" / "logs" / "dashboard"`, `HEARTBEAT_STALE_LOCAL = 120` (2 min), `HEARTBEAT_STALE_REMOTE = 300` (5 min)

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
   - Acquire `select_for_update` lock on PipelineProcess row (get_or_create by name) inside `transaction.atomic()`
   - Check phase conflict with `check_phase_conflict(name)` — reject if job running for same phase
   - Check PipelineProcess.status != running (or if running, verify PID is actually alive + cmdline matches)
   - Build argv via `build_argv(name, config_json)`
   - Spawn via `subprocess.Popen` with `preexec_fn=os.setpgrp`, `cwd=PROJECT_ROOT`, `env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}`. stdout/stderr to `LOG_DIR / f"{name}_{timestamp}.log"`
   - Record PID, started_at, log_file, status=running
   - Handle OSError/FileNotFoundError: mark failed with exception message in `error_message`, PID=null
   - **DB-update-failure on state transition**: retry once after 1s; if retry fails, log to stderr and kill subprocess via `os.killpg` to prevent untracked orphan
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
   - Job Queue summary: active/pending/completed counts. **Running jobs with stale heartbeat** (`last_heartbeat` > 2 min for local, > 5 min for remote) display a yellow warning badge
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
   - **Host selection**: In Iteration 1, all jobs are created with `host=socket.gethostname()` — the host running the dashboard instance. There is no operator-selectable host field. Jobs for other hosts are created by that host's own dashboard/orchestrator instance (each host runs its own `manage.py run_orchestrator`). All hosts share the same RDS job table, so the dashboard view shows all jobs from all hosts regardless of where they were created. A future iteration may add host targeting with an allowlist.
6. `pipeline_tags.py` — template filters: `duration` (timedelta → human-readable), `percentage` (current/total → percent string), `url_basename` (extracts filename from URL path via `urllib.parse.urlparse(url).path.rsplit('/', 1)[-1]` — used as primary identifier in reports tables per AC23), `stale_badge` (returns warning HTML class if `last_heartbeat` is older than threshold)
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
   - `RunStateForm`: multi-select state dropdown (all 50 + territories), phase checkboxes (seed, resolve — checked by default). **Note**: the spec's "Run State Convenience" section lists seed+resolve only; the Job Queue view section mentions crawl as well. We follow the "Run State Convenience" section (seed+resolve only) because the spec explicitly says crawl is managed separately on the Crawler Controls page. The crawl checkbox on `/jobs/` is omitted to avoid confusion. Expandable config panel: LLM model, brave QPS, consumer threads, etc.
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
   - **Stale heartbeat warning**: yellow badge if `running` and `last_heartbeat` > 2 min (local host) or > 5 min (remote host). Uses `stale_badge` template tag
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
   - Status panel: current job or ad-hoc process state, PID, uptime, progress. **Stale heartbeat warning badge** if running with old heartbeat (same thresholds as job detail)
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
2. `report_detail.html` — full detail: SHA, source URL (redacted), classification, confidence, year, size, first_page_text, page_count. S3 download link. **Note**: the spec says "full source URL" but the actual schema column is `source_url_redacted` (query params stripped for privacy). This is an intentional alignment to the existing schema — the redacted URL is sufficient for identifying the source page.
3. Views: `ReportListView`, `ReportDetailView`
4. S3 download view: accepts `content_sha256`, validates via ORM lookup (`Report.objects.get(content_sha256=sha)`), constructs S3 key as `pdfs/{sha}.pdf`, generates presigned URL via `boto3.client('s3').generate_presigned_url('get_object', ...)` with 300s expiry, returns HTTP redirect. **Dependency**: `boto3` is already installed (used by `lavandula.common.secrets` and the crawler's S3 archive). The S3 client uses the instance profile for credentials (same as the crawler). Bucket name sourced from `settings.S3_COLLATERAL_BUCKET = 'lavandula-nonprofit-collaterals'`. The signing code lives in `pipeline/views.py` in the `ReportDownloadView`

**Verify**: All control pages render, ad-hoc start/stop works, org/report browsers paginate and filter correctly, S3 download link works.

**ACs**: AC11, AC12, AC13, AC14, AC15, AC16, AC17, AC18, AC19, AC20, AC21, AC22, AC23, AC24

---

## Phase 8: Security Audit & Operational Setup

**Goal**: Verify all security requirements implemented in Phases 1-7 are correct, plus set up operational items.

**Note**: Most security requirements are implemented inline in their respective phases (auth in Phase 1/5, CSRF in Phase 5, command safety in Phase 3, log validation in Phase 4, audit logging in Phase 6/7). This phase is a systematic audit to catch gaps, plus operational setup.

**Audit checklist** (grep sanity checks + authoritative test-based verification):

Quick grep checks (catch obvious misses):
1. `grep -rL 'LoginRequiredMixin' pipeline/views.py` — every view class must include it
2. `grep 'DEBUG' dashboard/settings.py` — must be hardcoded `False`, not from env
3. `grep 'SESSION_COOKIE\|LOGOUT_REDIRECT' dashboard/settings.py` — verify all four session settings present
4. `grep 'hx-headers.*CSRFToken' pipeline/templates/pipeline/base.html` — CSRF token in HTMX headers
5. `grep -r 'shell=True' pipeline/` — must return zero results
6. `grep -r '|safe' pipeline/templates/` — must not apply to log content or error_message fields

**Authoritative test-based verification** (added to `test_views.py` in Phase 9):
- For every URL in `urls.py`, test unauthenticated GET → 302 to login
- For every POST endpoint, test without CSRF token → 403
- For every mutation endpoint (job create/cancel/retry, process start/stop), verify `PipelineAuditLog` entry created with correct action, process_name, parameters, and source_ip
- These tests are the actual acceptance gate; greps are a supplementary check

**Operational setup**:
1. `LOGOUT_REDIRECT_URL = '/login/'` in settings (implemented in Phase 1 but verified here)
2. Create `lavandula/dashboard/README.md` with setup instructions: `createsuperuser` (min 16-char password), `run_orchestrator`, SSH tunnel access
3. Log retention: add `management/commands/cleanup_logs.py` — deletes log files older than 30 days from `LOG_DIR`. Audit log rows (`PipelineAuditLog`) retained indefinitely.

**Verify**: All audit checks pass. Manual test: unauthenticated request → 302 to login. Shell injection attempt via form params → rejected by `COMMAND_MAP` validation.

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

### Integration Tests (CI-safe, no external dependencies)
7. `test_integration.py` — uses Django's test database only (no `lava_impact` access required). All subprocess calls mocked via `unittest.mock.patch('subprocess.Popen')`:
   - Job lifecycle: create → runner picks → (mock) subprocess completes → status updates
   - Dependency chain: seed completes → resolve becomes eligible → completes → no automatic crawl enqueued
   - Retry rewiring end-to-end: failed job retried → new pending created → dependents rewired → chain completes
   - Cancel cascade end-to-end: cancel running job → downstream dependents also cancelled
   - Concurrent duplicate submission: two `create_state_jobs` calls in rapid succession → second raises `DuplicateJobError` (verified at both application and DB constraint level)
   - Crawl job independence: `state_code=NULL`, no `depends_on`, immediately eligible without seed/resolve
   - Race-safety: duplicate rejection tests **must use `TransactionTestCase`** (not `TestCase`) to ensure PostgreSQL locking semantics are exercised. Test uses two threads submitting `create_state_jobs` concurrently for the same state+phase; asserts exactly one succeeds and the other raises `DuplicateJobError` or `IntegrityError` from the partial unique index

### Smoke Tests (require RDS connection, run manually)
8. `test_smoke.py` — decorated with `@unittest.skipUnless(os.environ.get('RUN_SMOKE_TESTS'), 'requires RDS')`:
   - Unmanaged models can query real `lava_impact` tables: `NonprofitSeed.objects.count()`, `Report.objects.count()`, `CrawledOrg.objects.count()`
   - PipelineRouter blocks writes: `NonprofitSeed.objects.create(...)` raises `RuntimeError`
   - S3 presigned URL generation works (requires instance profile)

**Verify**: `python manage.py test pipeline` passes (CI-safe tests only). `RUN_SMOKE_TESTS=1 python manage.py test pipeline.tests.test_smoke` passes on hosts with RDS access. Coverage on orchestrator, process manager, and command builder ≥80%.

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

### Round 1 — Plan Review (2026-04-26)

**Gemini**: Quota exhausted (RetryableQuotaError after 10 retries). Unable to review.

**Codex (REQUEST_CHANGES)** — 10 issues, all addressed:

1. **"Run State" phase checkboxes** (MEDIUM): Spec's Job Queue section says seed/resolve/crawl, but "Run State Convenience" section says seed/resolve only. Resolved: follow "Run State Convenience" (crawl managed separately). Added explicit note in Phase 6.
2. **Heartbeat cadence underspecified** (MEDIUM): Plan now specifies heartbeat writes every ~10s (poll cycle), well within spec's 30s maximum. Added stale heartbeat thresholds (2 min local, 5 min remote) as constants in Phase 3.
3. **Missing `cwd` and `PYTHONPATH`** (HIGH): Added `cwd=PROJECT_ROOT` and `PYTHONPATH` env var to both orchestrator (Phase 3) and process manager (Phase 4) subprocess launches.
4. **DB-update-failure handling missing** (HIGH): Added retry-once-then-kill behavior to both Phase 3 (orchestrator) and Phase 4 (process manager) for state transition DB writes.
5. **Stale heartbeat indicators missing** (MEDIUM): Added warning badges to dashboard (Phase 5), job detail (Phase 6), and pipeline control pages (Phase 7).
6. **AC23 URL basename implementation** (LOW): Added `url_basename` template filter to `pipeline_tags.py` (Phase 5) with specific implementation detail.
7. **S3 presigned URL dependencies underspecified** (MEDIUM): Clarified that `boto3` is already installed (used by secrets + crawler). Added bucket name config, instance profile credentials, and view location.
8. **Phase 8 too audit-oriented** (MEDIUM): Restructured Phase 8 as verification audit + operational setup. Security requirements are now implemented inline in Phases 1-7; Phase 8 runs automated grep checks and adds log cleanup command.
9. **Integration tests against real DB unreliable** (HIGH): Split Phase 9 into CI-safe tests (mocked subprocess, Django test DB only) and smoke tests (require RDS, gated by `RUN_SMOKE_TESTS` env var).
10. **Duplicate job race-safety** (HIGH): Added `transaction.atomic()` + `select_for_update` in `create_state_jobs`. Added partial unique index on `(state_code, phase)` filtered to `status IN ('pending', 'running')` as DB-level backstop.

### Round 2 — Red Team Security Review (2026-04-26)

**Codex Red Team (REQUEST_CHANGES)** — 6 findings, all addressed:

1. **NULL unique index hole for crawl jobs** (HIGH): PostgreSQL NULL values don't collide in unique indexes, so the partial unique index on `(state_code, phase)` didn't protect global phases (crawl/classify). Added second partial unique index on `(phase,)` filtered to `state_code IS NULL AND status IN ('pending', 'running')`.
2. **Phase selection behavior unspecified** (HIGH): `create_state_jobs` now documents all three allowed phase combinations: `['seed']`, `['resolve']`, `['seed', 'resolve']`. Dependency wiring adapts per combination. Invalid combinations rejected at form layer.
3. **Multi-host job creation ambiguous** (MEDIUM): Explicitly chose local-host-only submission in Iteration 1. `host=socket.gethostname()` always. No operator-selectable host field. Each host runs its own dashboard/orchestrator instance. All hosts share the same RDS job table.
4. **Report detail spec/model mismatch** (MEDIUM): Called out explicitly in Phase 7 that `source_url_redacted` (actual schema) is used instead of spec's "full source URL" — intentional alignment to existing schema for privacy.
5. **Concurrency tests underspecified** (MEDIUM): Race-safety tests now require `TransactionTestCase` with two threads and explicit `IntegrityError` assertions against PostgreSQL locking semantics.
6. **Phase 8 grep checks insufficient** (LOW): Kept greps as supplementary checks; added authoritative test-based verification in Phase 9 `test_views.py` as the actual acceptance gate for auth/CSRF/audit coverage.

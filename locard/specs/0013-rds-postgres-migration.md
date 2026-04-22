# Spec 0013 — SQLite → PostgreSQL (RDS) Dual-Write Migration

**Status**: draft  
**Protocol**: SPIDER  
**Priority**: high (start in parallel with 0006 Dashboard)  
**Date**: 2026-04-22  
**Depends on**: Spec 0004 (crawler), Spec 0007 (S3 archive)

---

## Problem

The pipeline currently stores all metadata in local SQLite files:

- `seeds.db` / `seeds-eastcoast.db` — nonprofit seed list + resolver output
- `reports.db` — PDF metadata, classifications, fetch_log, budget_ledger

SQLite served well through bootstrap and the TX 88-org validation, but
the near-term roadmap has three forcing functions for Postgres on RDS:

1. **Multi-user / multi-host access** — the dashboard (0006) will
   eventually be accessed from outside the EC2 host, and the future
   gallery app (0015) is inherently multi-user read-only. SQLite over
   NFS is not viable for either.

2. **Data protection** — EBS is a single point of failure. RDS
   automated backups + PITR + cross-AZ replicas give real durability
   guarantees.

3. **Downstream apps overlap with ongoing corpus build** — the
   extraction pipeline (0014) and gallery (0015) start before corpus
   build is "done" (there is no clean cutoff — corpus builds
   continuously). Neither can wait for a stop-the-world migration.

Option A (dual-write, staged) was chosen 2026-04-22 over clean-cutover
and sync-job alternatives. This spec implements it.

---

## Goals

1. Deploy a managed Postgres RDS instance with a clean schema that
   mirrors the current SQLite tables plus room to grow.
2. Add a dual-write mode to the DB-writer layer: every write hits
   SQLite (as today) AND RDS (async-queued, best-effort).
3. One-time backfill of existing SQLite rows into RDS at deploy time.
4. Once dual-write is stable (~2-4 weeks), flip reads to RDS. SQLite
   writes remain as a local cache for performance during active
   crawls; eventual retirement is out of scope here.
5. Use AWS IAM database authentication with 8-minute token refresh
   (matching the pattern in the elationfactory JS helper module).
6. Zero code downtime during any phase. Rollback = revert the read
   flip; SQLite still has full state.

---

## Non-Goals

- **Retiring SQLite entirely.** Post-flip, SQLite remains the primary
  write path for crawler hot loops; the migration stops at "reads
  live on RDS." A future spec can retire SQLite once multi-host
  writes are a real requirement.
- **Porting the JS IAM helper module.** We implement the core
  token-refresh pattern natively in Python using SQLAlchemy events
  — the architectural precedent is adopted; the JS code is not.
- **Schema-level feature expansion.** The RDS schema mirrors current
  SQLite columns 1:1 with Postgres-native types. JSONB, GIN indexes,
  partitioning, materialized views — all deferred.
- **Changing query access patterns in app code.** The crawler and
  classifier use the same SQLAlchemy session whether SQLite or
  Postgres is the backend. No per-call branching.
- **Multi-region or read-replica setup.** Single RDS instance until
  we actually have a use case.

---

## Design

### Phase model

Five phases, gated explicitly:

| Phase | What changes | Rollback |
|-------|-------------|----------|
| 0. **Provision** | RDS instance, DB, schema, roles, SG, SSM keys | Terminate RDS |
| 1. **Connect** | Python adapter (`lavandula.common.db`) with SQLAlchemy + IAM token manager | Remove adapter import |
| 2. **Backfill** | One-time copy of current SQLite rows into RDS | Truncate RDS tables |
| 3. **Dual-write** | `db_writer` writes to both SQLite and RDS | Flag off, drop RDS queue |
| 4. **Read flip** | Reads come from RDS; SQLite reads retained behind a fallback flag | Flip reads back to SQLite |

Each phase is independently mergeable and reversible. Phase order is
strict: cannot dual-write before backfill; cannot flip reads before
dual-write is validated.

### Phase 0 — RDS provisioning

**Target configuration** (recommended defaults; override at
provisioning time if needed):

| Setting | Value |
|---------|-------|
| Engine | Postgres 16.x |
| Instance class | `db.t4g.small` (2 vCPU, 2 GB RAM) |
| Storage | 20 GB gp3 SSD, auto-scale to 100 GB |
| Multi-AZ | No (single-AZ; research workload) |
| Backup retention | 7 days |
| Network | Same VPC as EC2, private subnet, no public access |
| AZ | us-east-1b (operator's choice; cross-AZ EC2↔RDS is acceptable) |
| IAM database auth | **Enabled** — this is the primary auth path |
| TLS | Required for all connections |
| Parameter group | Default `default.postgres16`; adjust if needed |
| Deletion protection | On |

**Database and role layout** (created once by admin, via psql after
first provisioning):

```sql
CREATE DATABASE lavandula
    WITH ENCODING = 'UTF8' LC_COLLATE = 'en_US.UTF-8';

-- Roles
CREATE ROLE lavandula_admin LOGIN;   -- DDL + migrations + backups
CREATE ROLE lavandula_app   LOGIN;   -- CRUD on app tables (crawler, dashboard)
CREATE ROLE lavandula_ro    LOGIN;   -- SELECT only (extraction, gallery)

-- IAM authentication: RDS-provided role that IAM-authenticated users inherit
GRANT rds_iam TO lavandula_app;
GRANT rds_iam TO lavandula_ro;
-- lavandula_admin keeps password auth (offline DDL); can optionally also have rds_iam

-- Schemas
\c lavandula
CREATE SCHEMA lavandula AUTHORIZATION lavandula_admin;
GRANT USAGE ON SCHEMA lavandula TO lavandula_app, lavandula_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE lavandula_admin IN SCHEMA lavandula
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lavandula_app;
ALTER DEFAULT PRIVILEGES FOR ROLE lavandula_admin IN SCHEMA lavandula
    GRANT SELECT ON TABLES TO lavandula_ro;
```

This gives us three clean roles with least-privilege separation:
`admin` runs migrations, `app` is the runtime role for the crawler
and other write paths, `ro` is for future extraction/gallery readers.

**Connection parameters stored in SSM**:

```
/cloud2.lavandulagroup.com/lavandula/rds/endpoint   → dbhost.us-east-1.rds.amazonaws.com
/cloud2.lavandulagroup.com/lavandula/rds/port       → 5432
/cloud2.lavandulagroup.com/lavandula/rds/database   → lavandula
/cloud2.lavandulagroup.com/lavandula/rds/app_user   → lavandula_app
/cloud2.lavandulagroup.com/lavandula/rds/ro_user    → lavandula_ro
```

Credentials for `lavandula_admin` (password auth for DDL work) live in
`/cloud2.lavandulagroup.com/lavandula/rds/admin_password` as
SecureString. `app` and `ro` roles use IAM auth — no password stored.

### Phase 1 — Python connection adapter

New module: `lavandula/common/db.py`.

Implements the IAM-token-as-password pattern from the JS helper
module (`@elationfactory/database-access`), condensed to the
SQLAlchemy idiom. ~40 LOC of substance; tests under 200 LOC.

#### Architecture (from JS helper precedent)

The JS helper module solved three specific anti-patterns:

1. **Pool-storm on token refresh** — naive code recreates the
   connection pool every 15 minutes when the IAM token expires. Our
   adapter avoids this by making `password` a *function* that's
   called per new connection, not per session.
2. **Mid-connection expiry** — the JS code uses `pool_pre_ping`
   equivalent to detect dead connections and open a fresh one with
   a current token. SQLAlchemy's `pool_pre_ping=True` handles this.
3. **Thread-safe token cache** — one fetch every ~8 minutes, shared
   across all threads. The Python equivalent uses a `threading.Lock`
   around the 8-min-TTL cache.

Token lifetime: AWS-issued tokens are valid 15 minutes. We refresh
at 8 minutes to give headroom. Same numbers as the JS helper.

#### Adapter API

```python
# lavandula/common/db.py

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
import boto3, threading, time

class IAMTokenManager:
    """Thread-safe IAM DB auth token cache with 8-min TTL.

    Mirrors the architecture of @elationfactory/database-access
    (JS). The pool is created once; connections get fresh tokens
    as they're established via the 'do_connect' SQLAlchemy event.
    """

    _REFRESH_AFTER_SEC = 8 * 60  # 8 min; AWS token lifetime is 15 min

    def __init__(self, *, region: str, host: str, port: int, user: str):
        self._rds = boto3.client("rds", region_name=region)
        self._region, self._host, self._port, self._user = (
            region, host, port, user
        )
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if time.time() < self._expires_at and self._token:
                return self._token
            self._token = self._rds.generate_db_auth_token(
                DBHostname=self._host,
                Port=self._port,
                DBUsername=self._user,
                Region=self._region,
            )
            self._expires_at = time.time() + self._REFRESH_AFTER_SEC
            return self._token


def make_engine(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    region: str,
    role: str = "app",  # "app" or "ro"
    pool_size: int = 5,
    max_overflow: int = 10,
) -> Engine:
    """Create a SQLAlchemy engine that authenticates via IAM DB auth.

    Connection flow:
    1. Engine + pool created once at startup. Pool lives forever.
    2. When a new physical connection is opened, the 'do_connect'
       event fires and injects a fresh IAM token as the password.
    3. Existing connections keep their original token until they
       close or fail a pre-ping, at which point they get a new one.

    No password is stored in env, config, or logs.
    """
    mgr = IAMTokenManager(region=region, host=host, port=port, user=user)

    # Build URL without password; injected per-connection below.
    url = (
        f"postgresql+psycopg2://{user}@{host}:{port}/{database}"
        f"?sslmode=require"
    )
    engine = create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,        # handles mid-connection token expiry
        pool_recycle=8 * 60,       # force recycle at 8 min
    )

    @event.listens_for(engine, "do_connect")
    def _inject_iam_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = mgr.token()

    return engine


def make_app_engine() -> Engine:
    """Production app-role engine. Reads connection params from SSM."""
    from .secrets import get_secret  # existing helper
    return make_engine(
        host=get_secret("rds/endpoint"),
        port=int(get_secret("rds/port")),
        database=get_secret("rds/database"),
        user=get_secret("rds/app_user"),
        region="us-east-1",
        role="app",
    )


def make_ro_engine() -> Engine:
    """Read-only engine for extraction/gallery future apps."""
    from .secrets import get_secret
    return make_engine(
        host=get_secret("rds/endpoint"),
        port=int(get_secret("rds/port")),
        database=get_secret("rds/database"),
        user=get_secret("rds/ro_user"),
        region="us-east-1",
        role="ro",
    )
```

#### Why not port the JS helper wholesale?

The JS helper is ~2000 LOC solving IAM auth + domain collections
(users/sessions/themes) + a CLI REPL + debug introspection. The
Python ecosystem already has most of that:

- IAM token generation: `boto3.rds.generate_db_auth_token()` (1 call,
  no need to reimplement SigV4)
- Pool management: SQLAlchemy built-in
- Pre-ping / recycle: SQLAlchemy config flags
- Debug REPL: `pgcli` (Python) or `psql` with the IAM token
- Query introspection: SQLAlchemy `inspect()`
- Domain collections: out of scope for this codebase

What remains unique to our case is ~40 LOC of token-cache glue.
Porting the whole module would be 50x the code for no additional
functionality. We adopt the *architecture* the JS helper defines
(password-as-function, per-connection token injection, 8-min cache,
pre-ping), not its implementation.

### Phase 2 — Schema + backfill

Alembic manages the schema. Initial migration mirrors current SQLite
tables exactly — same columns, same types (adjusted for Postgres
idioms: `INTEGER` → `BIGINT` where appropriate, `TEXT` → `TEXT`,
`BLOB` → `BYTEA` if any).

Tables in scope (copied from current SQLite):

- `nonprofits_seed`
- `reports`
- `fetch_log`
- `crawled_orgs`
- `budget_ledger`
- `runs` (seed enumeration audit)
- `deletion_log`

Constraints and indexes are carried over. `reports_public` view is
recreated in Postgres.

**Backfill tool**: `lavandula/common/tools/backfill_rds.py`.

Reads each SQLite DB in the project's `data/` directory, COPIES rows
to RDS via `psycopg2.extras.execute_values()` (fast bulk inserts).
Uses `ON CONFLICT DO NOTHING` per table so re-runs are idempotent.

Runs in two modes:
- `--dry-run`: counts rows that would be inserted; writes nothing.
- `--apply`: performs the inserts.

Per-table progress printed; per-row errors logged but don't abort
the run. Exit code 0 on success, 2 on hard error.

### Phase 3 — Dual-write

A new wrapper layer, `lavandula.reports.db_writer_dual`, wraps both
the existing SQLite DBWriter and a new RDS writer. Same public
interface; writes go to SQLite first (synchronous, fast), then
queued to RDS (async, best-effort).

RDS write failures are logged but do NOT fail the SQLite write.
This keeps the crawler hot path fast and resilient to RDS outages
during the stabilization period.

Feature flag `LAVANDULA_DUAL_WRITE` (env var, default off). When
off, behavior is pre-0013 identical. When on, RDS writes happen in
parallel with SQLite writes.

A background reconciler process runs periodically (manual for now,
cron later) to compare row counts between SQLite and RDS and flag
drift:

```
python -m lavandula.common.tools.verify_dual_write \
    --sqlite data/seeds.db \
    --rds-table nonprofits_seed
```

### Phase 4 — Read flip

After ~2-4 weeks of stable dual-write with minimal drift, flip
reads. Strategy:

1. Add `LAVANDULA_READ_BACKEND` env var (values: `sqlite` | `rds`;
   default `sqlite`).
2. All read paths (crawler seed selection, classifier fetch, future
   dashboard) consult this flag and use the appropriate engine.
3. Default stays `sqlite` during the dual-write stabilization.
4. To flip: set `LAVANDULA_READ_BACKEND=rds` in the environment
   where the crawler runs.
5. Rollback: unset the var. SQLite still has full state.

---

## Acceptance Criteria

### Phase 0 — Provision

**AC1** — RDS instance `lavandula-rds-prod` exists in us-east-1,
Postgres 16.x, IAM auth enabled, deletion protection on.

**AC2** — Security group allows ingress only from the EC2 instance
role's security group on port 5432.

**AC3** — Database `lavandula` exists with schema `lavandula`.
Three roles (`lavandula_admin`, `lavandula_app`, `lavandula_ro`)
exist with the permission grants specified in Design.

**AC4** — SSM parameters populated for endpoint/port/database/users.
`admin_password` is SecureString.

### Phase 1 — Connect

**AC5** — `lavandula.common.db.make_app_engine()` returns a working
SQLAlchemy engine that can SELECT against the `lavandula` database.
Verified via a smoke test that issues `SELECT 1`.

**AC6** — Token refresh works: after 8 minutes of idle time, a new
connection opens with a fresh IAM token (verified by monkey-patching
the clock in tests).

**AC7** — `pool_pre_ping=True` handles mid-connection token expiry:
a connection whose token has expired triggers a reconnect on next
use, not an exception.

**AC8** — The `do_connect` event correctly injects password from
the token manager; no password is passed via the URL or env.

**AC9** — `make_ro_engine()` connects as `lavandula_ro` and is
denied INSERT on any table (verified by a negative test).

### Phase 2 — Backfill

**AC10** — `backfill_rds.py --dry-run` counts rows without writing.
**AC11** — `backfill_rds.py --apply` copies rows with per-table progress.
**AC12** — Re-running `--apply` doesn't duplicate rows (`ON CONFLICT
DO NOTHING`).
**AC13** — Post-backfill row counts match between SQLite and RDS for
each table, verified by the `verify_dual_write` tool.

### Phase 3 — Dual-write

**AC14** — With `LAVANDULA_DUAL_WRITE=1`, a crawler run writes to
both SQLite and RDS; row counts stay in sync (±small backlog).
**AC15** — RDS write failure does NOT fail the SQLite write. The
crawler keeps running; the failure is logged with structured detail.
**AC16** — With the flag off, behavior is byte-identical to pre-0013.

### Phase 4 — Read flip

**AC17** — With `LAVANDULA_READ_BACKEND=rds`, crawler reads seeds
from RDS; query results match SQLite for the same filter.
**AC18** — Unsetting the flag reverts to SQLite reads.

---

## Traps to Avoid

1. **Don't read AWS credentials from env in the adapter.** boto3's
   default credential chain handles this via IMDS on EC2.

2. **Don't pass the IAM token via URL query string.** The password
   must go through `do_connect` kwargs so SQLAlchemy can apply it
   per-connection.

3. **Don't use `pool_recycle < 8*60`.** Recycling more often than the
   token-refresh cadence wastes connections without benefit.

4. **Don't disable `sslmode=require`.** IAM auth requires TLS.

5. **Don't assume RDS is always reachable during dual-write.** Failures
   must be non-fatal for the SQLite path. A temporary RDS outage
   should not stop the crawler.

6. **Don't grant `lavandula_ro` any INSERT/UPDATE/DELETE.** The whole
   point of the role is that it's *provably* read-only at the DB
   layer, not just by convention.

7. **Don't skip deletion protection on the RDS instance.** A
   developer-accidentally-running-Terraform-destroy scenario is the
   kind of thing we absolutely need to prevent.

8. **Don't store the admin password in git or env.** SSM SecureString
   only; fetched on-demand for DDL work.

9. **Don't run the backfill without a recent snapshot of SQLite.**
   Copy each `*.db` file to `*.db.pre-backfill.bak` first. The
   backfill is idempotent but the safeguard costs nothing.

10. **Don't skip the Phase 3 → Phase 4 stabilization period.** Flipping
    reads immediately after dual-write starts would miss drift
    signals. Wait ~2-4 weeks or until the reconciler reports zero
    drift over multiple crawl cycles.

---

## Security Considerations

### Threat model

- **Assets**: RDS credentials (IAM tokens + admin password), database
  contents (currently same as SQLite contents — not sensitive), the
  EC2 IAM role that issues tokens.
- **Actors**: Compromised EC2 host (could extract IAM role credentials
  and connect as `lavandula_app`); misconfigured security group
  (could expose RDS to the internet).

### Mitigations

1. **IAM auth, not passwords** for the app and ro roles. Tokens
   expire in 15 minutes; stolen tokens have bounded lifetime.
2. **No public RDS access**. Security group allows only the EC2
   role's SG. Private subnet.
3. **Least-privilege roles**: `app` gets DML on the `lavandula`
   schema only; `ro` gets SELECT only; `admin` is human-access for
   DDL (password, not used by applications).
4. **SSL required**: all connections use `sslmode=require`. Not
   `verify-full` for now (certificate rotation overhead); revisit
   if threat model tightens.
5. **Deletion protection**: on. `terraform destroy` / `aws rds
   delete-db-instance` require an explicit operator step to remove.
6. **Backup retention**: 7 days of automated PITR. Sufficient for
   operational recovery; not a compliance-grade retention window.

### Residual risks

- **EC2 host compromise** → attacker connects as `lavandula_app`, can
  read/write all app tables. Mitigations: the existing EC2 hardening
  + short-lived IAM tokens + RDS doesn't contain user PII.
- **Admin password leak** → DDL-level compromise. Mitigations:
  SecureString in SSM, scoped access, only used for manual migrations.
- **Schema migration drift** → Alembic must stay in sync with the
  Python models. Tests (part of Phase 2 builder work) verify this.

---

## Files Changed / Added

| Path | Status |
|------|--------|
| `lavandula/common/db.py` | NEW — IAM token manager + engine factories |
| `lavandula/common/tools/backfill_rds.py` | NEW — one-time SQLite→RDS copy |
| `lavandula/common/tools/verify_dual_write.py` | NEW — drift detection |
| `lavandula/reports/db_writer_dual.py` | NEW — dual-write wrapper |
| `lavandula/reports/db_writer.py` | EXTEND — accept optional RDS writer |
| `lavandula/alembic/` | NEW — migration environment + initial schema |
| `lavandula/common/tests/unit/test_db_adapter_0013.py` | NEW |
| `lavandula/common/tests/unit/test_backfill_0013.py` | NEW |
| `lavandula/reports/tests/unit/test_dual_write_0013.py` | NEW |

---

## Open Questions

1. **Admin IAM policy scope**: the admin policy needed for RDS
   provisioning (`rds:CreateDBInstance`, `rds:ModifyDBInstance`, etc.)
   is broader than the runtime policy. Attach temporarily, detach
   after provisioning — same pattern as the S3 bucket work.

2. **Connection pooling across crawler threads**: the crawler uses
   `ThreadPoolExecutor(max_workers=8)` (TICK-002). With `pool_size=5,
   max_overflow=10`, we have up to 15 concurrent connections from
   one crawler — comfortably under the `db.t4g.small` default of
   ~100. If we scale parallelism, revisit.

3. **Whether to adopt `psycopg` (v3) vs `psycopg2`**: psycopg v3 is
   the modern driver with async support, but SQLAlchemy's defaults
   and ecosystem compatibility favor psycopg2 for now. Recommend
   staying on psycopg2 through Phase 4; revisit for Phase 5+.

4. **Future retirement of SQLite** — out of scope for this spec but
   worth flagging: once extraction/gallery apps rely on RDS as the
   only source of truth, the SQLite layer becomes redundant. A
   future spec retires the dual-write, makes RDS primary, and keeps
   SQLite only for dev/CI.

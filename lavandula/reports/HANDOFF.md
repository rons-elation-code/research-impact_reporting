# Spec 0004 — Site-Crawl Report Catalogue — Operator Handoff

This module implements spec `locard/specs/0004-site-crawl-report-catalogue.md`.
It crawls nonprofit websites for annual/impact PDF reports, archives them
to content-addressable storage, classifies them via Anthropic Haiku, and
exposes a `reports_public` SQLite view for downstream consumers.

## Prerequisites

- **Python** 3.12+
- **Linux kernel** ≥ 5.10 with `kernel.unprivileged_userns_clone=1`.
  The PDF parser runs in a `CLONE_NEWUSER | CLONE_NEWNET` sandbox with
  seccomp-bpf. Engine refuses to start on non-Linux or a kernel that
  denies unprivileged user namespaces.
- **pyseccomp** installed (`pip install pyseccomp`). Missing →
  `HALT-sandbox-seccomp-missing.md`.
- **Encrypted volume** for `data/` and `raw/`. Detection order:
  1. `/proc/mounts` flag for LUKS / dm-crypt / ecryptfs.
  2. Operator-signed `.encrypted-volume` marker in the directory.

  Marker format (one line):

  ```
  This volume is encrypted by {scheme}; attested by {operator} on {iso8601}
  ```

  No detection → `HALT-encryption-not-detected.md` and exit 2.
- **ANTHROPIC_API_KEY** in env (never argv, never logged). `.env` file
  should be mode 0o600.
- **0001's `nonprofits.db`** at a known path; passed via
  `--nonprofits-db`.

## Install

```bash
cd lavandula/reports
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

## Lint

```bash
./lint.sh     # ruff (S rules) + bandit + verify=False ban + AC23 grep + pip-audit
```

## Run

```bash
export ANTHROPIC_API_KEY=...
python -m lavandula.reports.crawler \
    --nonprofits-db /path/to/nonprofits.db
```

Exit codes:
- `0` — completed normally
- `2` — halt (encryption / TLS / budget)
- `3` — another instance already holds the flock

Common flags:
- `--refresh` — re-process EINs already in `crawled_orgs`.
- `--retry-null-classifications` — only re-run classifier on rows with
  `classification IS NULL` from a prior outage.
- `--skip-tls-self-test` — ops override. Do NOT use in production.
- `--skip-encryption-check` — ops override. Do NOT use in production.

## Data layout

```
lavandula/reports/
  data/reports.db         (0o600)
  raw/<sha256>.pdf        (0o600 each; dir 0o700)
  logs/reports-crawler.log
  halt/HALT-*.md
```

## Query helpers

```python
from lavandula.reports import schema, catalogue

conn = schema.connect("data/reports.db", read_only=True)

for row in conn.execute("SELECT * FROM reports_public LIMIT 5"):
    print(dict(row))

latest = catalogue.latest_report_per_org(conn, ein="530196605")
```

## Deletion + retention

```python
from pathlib import Path
from lavandula.reports import catalogue

catalogue.delete(
    conn,
    content_sha256="...64hex...",
    reason="takedown_request",
    operator="ron",
    archive_dir=Path("raw"),
)

catalogue.sweep_stale(
    conn,
    archive_dir=Path("raw"),
    retention_days=365,
)
```

## Troubleshooting

- **`HALT-sandbox-userns-disabled.md`** — `kernel.unprivileged_userns_clone=0`
  on the host. `sudo sysctl -w kernel.unprivileged_userns_clone=1` and
  persist via `/etc/sysctl.d/`.
- **`HALT-sandbox-seccomp-missing.md`** — `pip install pyseccomp`.
- **`HALT-encryption-not-detected.md`** — create
  `data/.encrypted-volume` and `raw/.encrypted-volume` with the marker
  format above, OR mount those directories on a LUKS volume.
- **`HALT-classifier-budget.md`** — raise `CLASSIFIER_BUDGET_CENTS` in
  `config.py` or wait for the 24h budget window.
- **Exit code 3** — another crawler instance holds
  `.crawler.lock`; wait or check with `lsof`.

## Threat model (summary)

See spec 0004 §"Security Considerations" for the full mapping.
Controls in this module defend against:

- Seed-URL boundary violations (AC12.4)
- Cross-origin redirect hijacks (AC12.2, AC12.2.1)
- Hosting-platform authorship spoofing (AC12.3)
- SSRF incl. DNS rebinding + IPv4-mapped-IPv6 (AC12, AC12.1)
- PDF parser exploitation (AC14 sandbox)
- Active-content PDFs (AC15 — flagged, not refused; excluded from public view via AC23.1)
- Prompt injection via PDF first-page text (AC16.1 — untrusted_document
  wrapper + tool_use fixed schema + 0.8 confidence gate)
- URL credential leakage in logs / DB (AC13)
- Symlink TOCTOU on archive write (AC9 — O_NOFOLLOW + lstat pre/post)
- Budget overspend across crashes (AC18.1 reserve/settle/reconcile)

## Amendments

Any change that alters spec behavior requires a TICK amendment in
`locard/specs/0004-*.md`. Run the red-team review + plan consult
before landing non-trivial changes.

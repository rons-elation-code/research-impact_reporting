# lavandula/nonprofits — quick start

A throttled crawler that builds a queryable SQLite database of ~48K US
nonprofit profiles from Charity Navigator's public sitemap. See
`HANDOFF.md` for full operational details.

## Install

```bash
python -m venv venv
./venv/bin/pip install defusedxml requests beautifulsoup4 lxml pytest pytest-mock cryptography
```

(Python 3.12 required.)

## Run

```bash
# Full crawl (~48h wall-clock at 3s throttle):
./venv/bin/python -m lavandula.nonprofits.crawler

# Smoke test (limit of 50 EINs):
./venv/bin/python -m lavandula.nonprofits.crawler --limit 50

# Enumerate sitemap only, no profile fetches:
./venv/bin/python -m lavandula.nonprofits.crawler --no-download
```

Exit codes: 0 ok · 1 error · 2 halt-condition · 3 another process running.
On halt, inspect `logs/HALT-*.md` before restarting.

## Test

```bash
./venv/bin/python -m pytest lavandula/nonprofits/tests -q
```

## Report

After a crawl, generate a coverage report:

```bash
./venv/bin/python -m lavandula.nonprofits.report
# writes coverage_report.md next to this README
```

## Troubleshoot

- **"Another crawler process already holds ...crawler.lock"** — another
  instance is running. Check `ps`. Only remove the lock manually if you
  are sure no process owns it.
- **Halt `robots_disallow_ein`** — Charity Navigator has tightened their
  robots.txt; do NOT override. Pivot to their paid Data Feed (spec
  Approach 2).
- **Halt `challenge_detected`** — CN served a Cloudflare challenge body.
  Halt is intentional: investigate before resuming.
- **Halt `tls_selftest`** — TLS verification appears disabled. Check
  `REQUESTS_CA_BUNDLE` and any ambient cert config.

Full schema, query examples, contact protocol, and retention policy live
in `HANDOFF.md`.

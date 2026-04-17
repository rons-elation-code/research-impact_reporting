# lavandula/nonprofits — quick start

A throttled crawler that builds a queryable SQLite database of
Charity Navigator nonprofit profiles. Default scope (TICK-001):
~3K–7K rated orgs from CN's Best Charities index pages. A legacy
full-sitemap mode (~2.3M orgs, ~82 days at 3s throttle) is kept
behind `--source=sitemap` for reference use. See `HANDOFF.md` for
full operational details.

## Install

```bash
cd lavandula/nonprofits
python -m venv venv
# Production:
./venv/bin/pip install --require-hashes -r requirements.txt
# Development (adds pytest/pip-audit/bandit):
./venv/bin/pip install --require-hashes -r requirements-dev.txt
```

(Python 3.12 required; see `.python-version`.)

## Lint / security checks

```bash
./lint.sh   # pip-audit + bandit + verify=False scan
```

## Run

```bash
# Curated-lists crawl (~3K–7K orgs; default since TICK-001):
./venv/bin/python -m lavandula.nonprofits.crawler

# Smoke test (limit of 50 EINs):
./venv/bin/python -m lavandula.nonprofits.crawler --limit 50

# Enumerate only, no profile fetches:
./venv/bin/python -m lavandula.nonprofits.crawler --no-download

# Legacy: full XML sitemap (~2.3M orgs, not recommended):
./venv/bin/python -m lavandula.nonprofits.crawler --source sitemap
```

`--source` selects the seed enumeration strategy: `curated-lists`
(default) or `sitemap`. Rows for each source live in the same
`sitemap_entries` table but are prefixed (`curated:<slug>` vs
`Sitemap<N>.xml`) and are never crossed by the fetch scheduler.

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

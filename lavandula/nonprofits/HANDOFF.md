# Nonprofit Seed List — Operational Handoff

> Spec: `locard/specs/0001-nonprofit-seed-list-extraction.md`
> Plan: `locard/plans/0001-nonprofit-seed-list-extraction.md`

## What this is

Lavandula Design's internal catalogue of US nonprofits, built from
Charity Navigator's public data. Default seed source (since TICK-001)
is CN's Best Charities index pages (~3K–7K pre-rated orgs); a legacy
full-sitemap mode (~2.3M orgs, ~82 days to crawl at the 3s throttle) is
retained behind `--source=sitemap` for reference use. One SQLite row per
enumerated nonprofit. Fields include website URL (normalized), star
rating, overall score, revenue, expenses, program-expense ratio, NTEE
major/code, city, state, and a locally-stored mission statement (see
Usage Restrictions below).

The seed list feeds a downstream report-harvesting project that fetches
annual / impact report PDFs from each org's website.

## Schema

Canonical DDL lives in `schema.py`. Summary:

- `nonprofits` — one row per profile. Primary key `ein` (9-digit string).
- `fetch_log` — one row per HTTP attempt. Enum `fetch_status` captures
  retry/classification. Used for the 429-rate and halt-forensics.
- `sitemap_entries` — enumeration-time record of every EIN the seed
  source advertised. `source_sitemap` is prefixed `curated:<slug>` for
  rows enumerated from a Best Charities category page, or
  `Sitemap<N>.xml` for rows from the legacy XML sitemap. The fetch
  scheduler partitions by this prefix (via `--source`) so a DB
  populated by a prior run in one mode does not leak into a run in the
  other. Join against `nonprofits` to find un-fetched EINs.

### Key columns

| Column | Nullability | Notes |
|---|---|---|
| `ein` | never | 9 digits, no dashes, validated with `canonicalize_ein`. |
| `website_url` | may be NULL | Normalized. NULL means unusable; see `website_url_reason`. |
| `website_url_raw` | may be NULL | Exactly as scraped. |
| `website_url_reason` | NULL if url set | One of: `missing`, `mailto`, `tel`, `social`, `unwrap_failed`, `invalid`. |
| `rating_stars` | NULL if unrated | 1-4 integer. |
| `parse_status` | never | `ok` / `partial` / `blocked` / `challenge` / `unparsed`. |
| `redirected_to_ein` | NULL if same | Populated when the source EIN 30x-redirected to another EIN. |
| `content_sha256` | never | SHA256 of raw HTML at last fetch. Delta detection. |
| `parse_version` | never | Incremented when the extractor changes; re-extract rows with lower values. |

## How to run

```bash
# From repo root:
python -m lavandula.nonprofits.crawler                       # curated (default)
python -m lavandula.nonprofits.crawler --limit 50            # smoke test
python -m lavandula.nonprofits.crawler --no-download         # enumerate only
python -m lavandula.nonprofits.crawler --refresh             # re-fetch everything
python -m lavandula.nonprofits.crawler --source sitemap      # legacy full sitemap
```

`--source` selects the seed enumeration strategy:

- `curated-lists` (default): scrape CN's `/discover-charities/best-charities/*`
  category pages. Expected scale: 3K–7K rated orgs. Wall-clock at 3s
  throttle: roughly 3–6 hours.
- `sitemap`: legacy full XML sitemap. ~2.3M orgs across 48 child
  sitemaps. Retained for reference; not a recommended v1 run mode
  (~82 days wall-clock).

Exit codes: `0` clean, `1` generic error, `2` halt condition fired (see
`logs/HALT-*.md`), `3` another process already holds the lock.

## Example queries

```sql
-- Prime prospects: 4-star orgs with ≥ $5M revenue
SELECT ein, name, website_url, total_revenue
FROM nonprofits
WHERE rating_stars = 4
  AND total_revenue >= 5000000
  AND website_url IS NOT NULL
ORDER BY total_revenue DESC;

-- Sector-targeted list (arts orgs in NY)
SELECT ein, name, website_url
FROM nonprofits
WHERE ntee_major = 'A' AND state = 'NY';

-- Dedup redirect pairs
SELECT COALESCE(redirected_to_ein, ein) AS canonical_ein,
       MAX(name) AS name,
       MAX(website_url) AS website_url
FROM nonprofits
GROUP BY canonical_ein;

-- 429 rate (spec Operational metric)
SELECT
  (SELECT COUNT(*) FROM fetch_log WHERE fetch_status='rate_limited') * 1.0
    / (SELECT COUNT(DISTINCT url) FROM fetch_log) AS post_retry_429_rate;
```

## How to refresh

A full re-crawl is operator-initiated:

```bash
python -m lavandula.nonprofits.crawler --refresh
```

This re-fetches every EIN already in the DB (for the active `--source`),
overwriting the raw archive. Delta detection is handled in-DB via
`content_sha256` and `last_fetched_at`. We do NOT keep per-run
snapshots in v1; if that becomes needed, file a new spec.

To pick up newly-advertised EINs only:

```bash
# Remove ONLY the rows for the active source, then re-run.
# Curated (default):
sqlite3 lavandula/nonprofits/data/nonprofits.db \
  "DELETE FROM sitemap_entries WHERE source_sitemap LIKE 'curated:%';"
# Legacy sitemap:
sqlite3 lavandula/nonprofits/data/nonprofits.db \
  "DELETE FROM sitemap_entries WHERE source_sitemap NOT LIKE 'curated:%';"
```

The crawler will re-enumerate its source and only fetch EINs not already
in `nonprofits`.

To rotate the checkpoint HMAC key (e.g., suspected leak):

```bash
rm lavandula/nonprofits/data/.crawler.key
# Next run regenerates; existing checkpoints fail MAC verification and
# are rotated to `checkpoint.corrupt-*.json` (retained up to 5, then
# oldest is deleted).
```

## Contact protocol

If Charity Navigator (CN) reaches out to Ron about this crawler:

1. **Immediate halt.** Send SIGTERM to the crawler process; it flushes its
   checkpoint and writes `logs/HALT-provider-complaint-*.md`.
2. Preserve `fetch_log`, archive, and all HALT files for post-incident
   review.
3. Decide on retention (keep as-is, prune, or delete) based on the nature
   of the objection.
4. Only restart after written resolution. Default response to a request
   to stop scraping: pivot to Charity Navigator's paid Data Feed
   (Approach 2 in the spec).
5. Document the incident in `lavandula/nonprofits/incidents/{date}-{subject}.md`.

## Retention

The raw HTML archive (`raw/cn/`) contains CN-rendered officer/director
names and compensation numbers (we do NOT extract these into DB columns,
but they live in the archived HTML). Posture:

- Archive directory: `0o700`; files `0o600`.
- Internal use only. NO cloud backup. NO sharing outside Ron's direct
  admin access.
- Once the downstream report-harvesting project has consumed the DB,
  the raw archive becomes a deletion candidate. Document the decision
  in this file.

Retention decision: _pending_ (to be filled in post-downstream-consumption).

## Usage restrictions

Derived from spec § Legal / Compliance Constraints:

- **`mission` field** — internal segmentation only. Never shown to any CN
  competitor. Never included in any public Lavandula output. Never
  exported outside the internal DB. If this posture changes, re-verify
  source terms first.
- **`rated`, `rating_stars`, `overall_score`, `beacons_completed`** — may
  be used for internal segmentation. Must NOT be republished or presented
  as Lavandula's own ratings. Any external output that references a CN
  rating must attribute it to Charity Navigator.
- **Raw HTML archive** — local-only. No cloud upload. No sharing outside
  Ron's direct admin access.
- **Officer/director names and compensation** — present in the raw HTML
  but intentionally NOT extracted. Do not extract them in a derivative
  project without reviewing the PII posture.

## Agent-based URL resolution (Spec 0008)

Preferred for 1K+ org batches. Uses Claude Code sub-agents restricted to
WebSearch + WebFetch only. Results are ingested into `nonprofits_seed`
via SQLAlchemy. A resumable per-run manifest tracks batch state; an
advisory flock guards the run directory against concurrent runners.

Basic usage:

    python -m lavandula.nonprofits.tools.batch_resolve \
        --db data/seeds.db \
        --state NY \
        --max-orgs 500 \
        --batch-size 50 \
        --parallelism 2 \
        --model haiku

Resume a killed run:

    python -m lavandula.nonprofits.tools.batch_resolve \
        --resume data/seeds.db-agent-results/run-2026-04-22T14-00-00-a1b2c3

Dry-run (cost preview only, no agents spawned):

    python -m lavandula.nonprofits.tools.batch_resolve --db data/seeds.db --dry-run

## Incidents log

`lavandula/nonprofits/incidents/` — populated only when an incident
occurs. Format: `{YYYY-MM-DD}-{subject}.md`.

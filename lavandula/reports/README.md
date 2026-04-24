# lavandula.reports — site-crawl report catalogue

Implementation of [`locard/specs/0004-site-crawl-report-catalogue.md`].

Given a seed list of US nonprofits (from 0001), crawl each org's
website for annual / impact / transparency PDF reports, archive each
one to content-addressable storage, extract first-page text in a
sandboxed subprocess, classify via Anthropic Haiku, and expose a
queryable `reports_public` SQLite view.

See [`HANDOFF.md`](./HANDOFF.md) for install + run instructions.

## Taxonomy

Crawler keyword lists, signal weights, and thresholds live in
`lavandula/docs/collateral_taxonomy.yaml` — the single source of truth.
Changes take effect on next crawler start (no code changes needed).

To tune keywords or thresholds:

1. Edit `collateral_taxonomy.yaml` (add/remove keywords, adjust weights)
2. Run the validator: `python -c "from lavandula.reports.taxonomy import load_taxonomy; load_taxonomy(Path('lavandula/docs/collateral_taxonomy.yaml'))"`
3. Run the offline baseline grader: `python -m lavandula.reports.tools.grade_baseline`
4. Run tests: `python -m pytest lavandula/reports/tests/ -q`
5. Open a PR — CI runs the validator and Fordham regression automatically

Per-candidate decision data is logged to `logs/crawler_decisions.jsonl`
(JSONL, daily rotation, 90-day retention). Use this to diagnose
false-positive/negative patterns after a crawl run.

## Module map

| Module | Responsibility |
|--|--|
| `config.py` | Throttles, caps, paths, UA, model IDs |
| `http_client.py` | Throttled HTTPS client + TLS self-test + size caps |
| `url_guard.py` | AC12 SSRF + AC12.1 DNS pinning |
| `url_redact.py` | AC13 redaction + AC25 canonicalization |
| `redirect_policy.py` | AC12.2 / AC12.2.1 per-hop gating |
| `robots.py` | AC1 robots.txt with 24h cache |
| `sitemap.py` | AC8.1 defusedxml sitemap parser |
| `taxonomy.py` | YAML taxonomy loader + Pydantic validator |
| `filename_grader.py` | Filename heuristic scoring for three-tier triage |
| `decisions_log.py` | Per-candidate JSONL decision log with URL redaction |
| `candidate_filter.py` | AC2 / AC3 / AC4 / AC12.3 link filter + triage |
| `discover.py` | Per-org orchestration of robots → homepage → subpages |
| `fetch_pdf.py` | AC7 HEAD+GET + magic-byte + structural pre-check |
| `archive.py` | AC9 symlink-safe atomic write + AC10 dedup |
| `pdf_extract.py` | Active-content detector + metadata sanitizer |
| `sandbox/runner.py` | AC14 userns + netns + seccomp + rlimits |
| `sandbox/pdf_extractor.py` | Untrusted PDF parsing payload |
| `classify.py` | AC16 / AC16.1 / AC17 Haiku tool-use classifier |
| `budget.py` | AC18 / AC18.1 atomic reserve/settle/release ledger |
| `schema.py` | DDL + `reports_public` view |
| `db_writer.py` | Parameterized writes (whitelisted for `FROM reports`) |
| `catalogue.py` | AC22 delete + AC22.1 sweep + AC24 latest-per-org |
| `crawler.py` | Main loop + AC19 flock + AC20 resume + AC21.1 encryption |
| `report.py` | `coverage_report.md` generator |

## Status

All 26 gating ACs from the spec are implemented and tested
(`lavandula/reports/tests/`). See
`locard/tests/0004-site-crawl-report-catalogue/README.md` for the
AC-to-test map.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt -r requirements.txt
python -m pytest lavandula/reports/tests -q
./lavandula/reports/lint.sh
```

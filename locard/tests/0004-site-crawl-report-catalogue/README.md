# Spec 0004 — Acceptance Tests

Per the SPIDER / TDD discipline, every acceptance criterion in
`locard/specs/0004-site-crawl-report-catalogue.md` has at least
one automated test. Tests live under
`lavandula/reports/tests/{unit,integration}/`.

## AC → test file map

| AC   | Test file                                                         | Phase |
|------|-------------------------------------------------------------------|-------|
| AC1  | `unit/test_robots.py`                                             | 2     |
| AC2  | `unit/test_candidate_filter.py`                                   | 2     |
| AC3  | `unit/test_candidate_filter.py`                                   | 2     |
| AC4  | `unit/test_candidate_filter.py`                                   | 2     |
| AC5  | `integration/test_discover.py`                                    | 2     |
| AC6  | `integration/test_fetch.py`                                       | 3     |
| AC7  | `unit/test_fetch.py`                                              | 3     |
| AC8  | `integration/test_http_client.py`                                 | 1     |
| AC8.1| `unit/test_sitemap.py` + `unit/test_candidate_filter.py`          | 2     |
| AC9  | `integration/test_archive.py`                                     | 3     |
| AC10 | `integration/test_archive.py`                                     | 3     |
| AC11 | `integration/test_http_client.py`                                 | 1     |
| AC12 | `integration/test_url_guard.py`                                   | 1     |
| AC12.1 | `integration/test_url_guard.py`                                 | 1     |
| AC12.2 | `integration/test_redirect_policy.py`                           | 1     |
| AC12.2.1 | `integration/test_redirect_policy.py`                         | 1     |
| AC12.3 | `unit/test_candidate_filter.py`                                 | 2     |
| AC12.4 | `unit/test_crawler.py`                                          | 6     |
| AC13 | `unit/test_url_redact.py`                                         | 1     |
| AC14 | `integration/test_sandbox.py`                                     | 4     |
| AC15 | `unit/test_pdf_extract.py`                                        | 4     |
| AC16 | `unit/test_classify.py`                                           | 5     |
| AC16.1 | `unit/test_classify.py`                                         | 5     |
| AC16.2 | `integration/test_classify.py`                                  | 5     |
| AC17 | `unit/test_classify.py`                                           | 5     |
| AC18 | `integration/test_classify.py`                                    | 5     |
| AC18.1 | `unit/test_budget.py`                                           | 5     |
| AC18.2 | `unit/test_pdf_extract.py`                                      | 4     |
| AC19 | `integration/test_cli.py`                                         | 6     |
| AC20 | `integration/test_cli.py`                                         | 6     |
| AC21 | `integration/test_cli.py`                                         | 6     |
| AC21.1 | `integration/test_cli.py`                                       | 6     |
| AC22 | `integration/test_catalogue.py`                                   | 6     |
| AC22.1 | `integration/test_catalogue.py`                                 | 6     |
| AC23 | `unit/test_catalogue.py`                                          | 6     |
| AC23.1 | `unit/test_schema.py`                                           | 1     |
| AC24 | `unit/test_catalogue.py`                                          | 6     |
| AC25 | `unit/test_url_redact.py`                                         | 1     |
| AC26 | `integration/test_schema_drift.py`                                | 6     |

Each gating AC has an integration test or a fixture-driven unit test.

## Running

```bash
cd /home/ubuntu/research/.builders/0004
. .venv/bin/activate
python -m pytest lavandula/reports/tests/ -q
```

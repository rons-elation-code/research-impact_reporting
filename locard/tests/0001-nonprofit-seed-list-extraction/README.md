# TDD Acceptance Tests — Spec 0001

This directory is a spec-workflow marker only. The actual test suite lives
under `lavandula/nonprofits/tests/` to keep pytest collection tied to the
implementation package. See the plan's Acceptance Test Matrix for the
AC → test file mapping:

| AC | Test file |
|----|-----------|
| AC1  | `lavandula/nonprofits/tests/unit/test_schema.py` |
| AC2  | `lavandula/nonprofits/tests/integration/test_http_client.py::test_throttle_enforces_min_interval` |
| AC4  | `lavandula/nonprofits/tests/integration/test_http_client.py::test_size_capped, test_gzip_decompressed_cap` |
| AC5, 5a | `.../test_http_client.py::test_cross_host_redirect_blocked, test_scheme_downgrade_redirect_blocked` |
| AC5b | `.../test_http_client.py::test_unexpected_content_type_blocked` |
| AC6  | `.../test_http_client.py::test_cookies_not_persisted` |
| AC6a | `lavandula/nonprofits/tests/unit/test_http_client.py::test_retry_after_http_date_future` |
| AC7, 7-tied | `.../unit/test_robots.py` |
| AC8, 8a | `.../unit/test_sitemap_parse.py::test_xxe_entity_not_resolved, test_xxe_ssrf_does_not_fetch` |
| AC8b | `.../unit/test_extract.py::test_xxe_html_not_resolved` |
| AC9, AC10 | `.../unit/test_sitemap_parse.py` |
| AC11 | `.../unit/test_robots.py::test_disallowed_ein_is_blocked` + `.../unit/test_url_utils.py` |
| AC12, 15, 15a, 15b | `.../integration/test_archive.py` |
| AC13, 14, 14a | `.../integration/test_fetcher.py` |
| AC14b | `.../unit/test_db_writer.py::test_redirected_dedup_query` |
| AC16-17 | `.../unit/test_extract.py` |
| AC18-19 | `.../unit/test_url_normalize.py` |
| AC20 | `.../unit/test_db_writer.py::test_sql_injection_mission_roundtrip` |
| AC21 | `.../unit/test_log_sanitize.py` + `.../unit/test_db_writer.py::test_log_crlf_sanitized_in_db` |
| AC22 | `.../integration/test_crawler_lock.py` |
| AC24, 24a, 24b | `.../unit/test_checkpoint.py` |
| AC25-28 | `.../unit/test_stop_conditions.py` |
| AC31 | `.../integration/test_report.py` |

A subset of ACs (AC3, AC23, AC29, AC30a-c, AC33) depend on live network
or signal-handling harnesses not wired into unit CI. Those are documented
in `locard/tests/0001-.../DEFERRED.md` below.

# Deferred Acceptance Tests (Spec 0001)

These ACs require external infrastructure or signal handling beyond the
scope of a hermetic unit / integration run. They are covered by manual
validation (Phase 7) and operator inspection.

- **AC3** — TLS self-test against `expired.badssl.com`. Covered by
  `http_client.tls_self_test()` which starts a local expired-cert server
  dynamically. Not exercised in CI because it depends on `cryptography`
  (present in venv but optional) and on a remote host the CI runner may
  not reach.
- **AC23, AC29, AC29a** — Checkpoint resume + SIGTERM shutdown. Exercised
  manually in Phase 7 via a 50-EIN live run; requires a full crawler
  process and a kill signal.
- **AC27** — Disk-space < 5 GB halt. Covered indirectly by
  `StopConditions.evaluate()` checking `shutil.disk_usage`. A real
  integration test would need to fill a partition.
- **AC30, 30a-c** — post-run file permissions, DNS IP-pin drift, clock
  skew, content-length sanity. Permissions are enforced in-code
  (`archive.write_file`, `schema.ensure_db`). Drift/skew checks emit
  warnings only and are inspected manually.
- **AC33** — 50-EIN live validation run. Operator task, not a CI test.

The full spec 0001 acceptance gates are met in a combination of these
deferred manual checks and the automated suite in
`lavandula/nonprofits/tests/`.

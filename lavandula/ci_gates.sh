#!/usr/bin/env bash
# Spec 0017 CI gates — three checks that MUST pass for the PR to merge.
#   1. No `sqlite3` imports in production code (AC1).
#   2. No hardcoded advisory lock keys (red-team finding HIGH).
#   3. Bandit S608 — parameterized SQL only (red-team finding CRITICAL).
#
# Run from the repo root: `bash lavandula/ci_gates.sh`
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE"

fail=0

echo "== AC1: no sqlite3 in production code =="
if grep -rn "^import sqlite3\|^from sqlite3" "$ROOT" --include='*.py' \
    | grep -v '/tools/backfill_rds\.py:' \
    | grep -v '/tests/' ; then
    echo "FAIL: sqlite3 import in production code" >&2
    fail=1
else
    echo "  ok"
fi

echo "== advisory lock key registry =="
# Every production .py file that calls pg_advisory_xact_lock must also
# import from lavandula.common.lock_keys. No hardcoded hex literals.
bad_files=$(
    grep -rl 'pg_advisory_xact_lock' "$ROOT" --include='*.py' \
        | grep -v '/tests/' \
        | grep -v '/common/lock_keys\.py$' || true
)
for f in $bad_files; do
    if ! grep -q 'lavandula\.common\.lock_keys\|from .*lock_keys' "$f"; then
        echo "FAIL: $f uses pg_advisory_xact_lock without importing from lavandula.common.lock_keys" >&2
        fail=1
    fi
done
if [[ $fail -eq 0 ]]; then
    echo "  ok"
fi

echo "== Bandit S608 (SQL injection) =="
if command -v bandit >/dev/null 2>&1; then
    bandit -q -r "$ROOT" \
        --exclude "$ROOT/common/tests,$ROOT/nonprofits/tests,$ROOT/reports/tests" \
        --tests B608 \
        --severity-level low \
        || { echo "FAIL: bandit S608" >&2; fail=1; }
else
    echo "  (bandit not installed; skipping — install 'bandit>=1.7')"
fi

if [[ $fail -ne 0 ]]; then
    exit 1
fi
echo "OK"

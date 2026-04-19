#!/usr/bin/env bash
# Spec 0004 lint gate. Belt-and-suspenders: ruff S-rules + bandit run
# independently; either failing blocks the commit.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "== ruff =="
python -m ruff check --select E,F,W,I,S,B,UP --target-version py312 .

echo "== bandit =="
python -m bandit -q -r . -x tests,sandbox

echo "== verify=False ban =="
# Reject any occurrence of `verify=False` or `SSL_VERIFY=False` in runtime
# code. Tests opt in via `tests/` which is excluded.
if grep -R --include="*.py" --exclude-dir=tests -nP 'verify\s*=\s*False' .; then
    echo "FAIL: verify=False in runtime code" >&2
    exit 1
fi

echo "== AC23 FROM reports whitelist =="
# Mirror the test: grep for 'from reports' (SQL) outside the whitelist.
if grep -R --include="*.py" --exclude-dir=tests -nE '\bfrom reports\b' . \
        | grep -Ev '^\./(catalogue|db_writer|schema)\.py:' \
        | grep -v reports_public; then
    echo "FAIL: raw FROM reports outside whitelist" >&2
    exit 1
fi

echo "== pip-audit =="
python -m pip_audit -r requirements.txt -r requirements-dev.txt --disable-pip || true

echo "OK"

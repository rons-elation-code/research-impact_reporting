#!/usr/bin/env bash
# Supply-chain + static security checks for the nonprofit crawler.
# Exit non-zero on any HIGH/CRITICAL finding so this script can run in CI.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY="${PY:-./venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "lint: Python venv not found at $PY; run 'python -m venv venv && ./venv/bin/pip install -r requirements-dev.txt' first" >&2
  exit 2
fi

echo "== pip-audit (CVE scan against lockfile) =="
"$PY" -m pip_audit -r requirements.txt --strict --disable-pip

echo "== bandit (static security rules) =="
"$PY" -m bandit -q -r . \
  --exclude "./venv,./tests,./__pycache__,./raw,./data,./logs" \
  --severity-level medium \
  --confidence-level medium

echo "== verify==False scan (plan Phase 1 HIGH-5) =="
if grep -RIn --include='*.py' --exclude-dir=tests --exclude-dir=venv 'verify\s*=\s*False' .; then
  echo "lint: verify=False is forbidden per spec 0001 § Security." >&2
  exit 1
fi

echo "== ok =="

#!/bin/bash
# nptechforgood nightly crawler — runs posts, then pages, then extraction.
# Called by cron at 04:00 UTC (11pm Central). Auto-kills at 10:00 UTC (5am Central).
set -euo pipefail

DIR="/home/ubuntu/research/nptech"
VENV="$DIR/venv/bin/python"
LOG="$DIR/logs/nightly-$(date +%Y-%m-%d).log"

exec >> "$LOG" 2>&1
echo "=== nightly crawl start: $(date -u) ==="

# Posts backfill (resumes from checkpoint). timeout kills it at 5h50m
# leaving 10 min for pages + extraction.
timeout 21000 "$VENV" "$DIR/crawler.py" || true

echo "=== posts phase done: $(date -u) ==="

# Pages endpoint (skips already-fetched)
timeout 1800 "$VENV" "$DIR/crawler.py" --pages-endpoint || true

echo "=== pages phase done: $(date -u) ==="

# Extraction pass over everything new
"$VENV" "$DIR/extract.py"

echo "=== extraction done: $(date -u) ==="
echo "=== nightly crawl complete: $(date -u) ==="

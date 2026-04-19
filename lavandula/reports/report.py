"""Coverage report generator (Phase 6 deliverable).

Reads `reports_public` (AC23) + `fetch_log` aggregates and emits a
Markdown summary for operator review. Produced after each full pass.
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path


def _table(rows: list[tuple]) -> str:
    if not rows:
        return "_(none)_\n"
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for r in rows:
        lines.append(
            "| " + " | ".join(str(v).ljust(w) for v, w in zip(r, widths)) + " |"
        )
    return "\n".join(lines) + "\n"


def generate(conn: sqlite3.Connection, out: Path) -> Path:
    """Write a coverage_report.md file. Returns its path."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

    total_public = conn.execute("SELECT COUNT(*) FROM reports_public").fetchone()[0]
    by_class = list(conn.execute(
        "SELECT classification, COUNT(*) FROM reports_public "
        "GROUP BY classification ORDER BY 2 DESC"
    ))
    by_platform = list(conn.execute(
        "SELECT hosting_platform, COUNT(*) FROM reports_public "
        "GROUP BY hosting_platform ORDER BY 2 DESC"
    ))
    by_year = list(conn.execute(
        "SELECT report_year, COUNT(*) FROM reports_public "
        "GROUP BY report_year ORDER BY 1 DESC"
    ))
    fetch_outcomes = list(conn.execute(
        "SELECT fetch_status, COUNT(*) FROM fetch_log GROUP BY fetch_status ORDER BY 2 DESC"
    ))
    crawled = conn.execute("SELECT COUNT(*) FROM crawled_orgs").fetchone()[0]

    body = f"""# Spec 0004 — Coverage Report

Generated: `{now}`

## Totals

- Orgs processed: **{crawled}**
- Reports in `reports_public` (attribution + confidence + active-content clean): **{total_public}**

## By classification

{_table(by_class)}

## By hosting platform

{_table(by_platform)}

## By year

{_table(by_year)}

## Fetch outcomes

{_table(fetch_outcomes)}

---
_`reports_public` excludes `platform_unverified`, low-confidence,
and active-content rows per AC12.3 / AC16.2 / AC23.1._
"""
    out.write_text(body)
    return out


__all__ = ["generate"]

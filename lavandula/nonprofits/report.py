"""Generate coverage_report.md from a populated nonprofits DB.

All queries read-only (PRAGMA query_only). The report is informational;
field-population percentages below 50% trigger a manual review upstream,
not an automated failure.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from pathlib import Path

from . import config, schema


def _pct(n: int, d: int) -> str:
    if d == 0:
        return "n/a"
    return f"{(n / d * 100):.1f}%"


def generate(db_path: Path | str = config.DB_PATH) -> str:
    """Return the coverage report markdown."""
    # Ensure schema exists so an early report-run on a fresh DB does not crash.
    schema.ensure_db(db_path).close()
    conn = schema.connect(db_path, read_only=True)
    try:
        enumerated = conn.execute("SELECT COUNT(*) FROM sitemap_entries").fetchone()[0]
        fetched = conn.execute("SELECT COUNT(*) FROM nonprofits").fetchone()[0]
        fetch_log_rows = conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]

        ok = conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE fetch_status='ok'"
        ).fetchone()[0]
        rate_limited = conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE fetch_status='rate_limited'"
        ).fetchone()[0]
        challenge = conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE fetch_status='challenge'"
        ).fetchone()[0]
        not_found = conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE fetch_status='not_found'"
        ).fetchone()[0]
        distinct_urls = conn.execute(
            "SELECT COUNT(DISTINCT url) FROM fetch_log"
        ).fetchone()[0]

        # Hardcoded column → query mapping (no string interpolation on user
        # input — bandit B608 compliant). Adding a column here is a deliberate
        # schema change.
        _POP_QUERIES = {
            "website_url": "SELECT COUNT(*) FROM nonprofits WHERE website_url IS NOT NULL",
            "rating_stars": "SELECT COUNT(*) FROM nonprofits WHERE rating_stars IS NOT NULL",
            "total_revenue": "SELECT COUNT(*) FROM nonprofits WHERE total_revenue IS NOT NULL",
            "state": "SELECT COUNT(*) FROM nonprofits WHERE state IS NOT NULL",
            "mission": "SELECT COUNT(*) FROM nonprofits WHERE mission IS NOT NULL",
        }

        def pop(col: str) -> int:
            return conn.execute(_POP_QUERIES[col]).fetchone()[0]

        website = pop("website_url")
        rating = pop("rating_stars")
        revenue = pop("total_revenue")
        state = pop("state")
        mission = pop("mission")

        by_state = conn.execute(
            """
            SELECT state, COUNT(*) AS n FROM nonprofits
            WHERE state IS NOT NULL
            GROUP BY state ORDER BY n DESC LIMIT 20
            """
        ).fetchall()
        by_ntee = conn.execute(
            """
            SELECT ntee_major, COUNT(*) AS n FROM nonprofits
            WHERE ntee_major IS NOT NULL
            GROUP BY ntee_major ORDER BY n DESC LIMIT 20
            """
        ).fetchall()
        by_rating = conn.execute(
            """
            SELECT rating_stars, COUNT(*) AS n FROM nonprofits
            GROUP BY rating_stars ORDER BY rating_stars DESC
            """
        ).fetchall()
        parse_status = conn.execute(
            """
            SELECT parse_status, COUNT(*) AS n FROM nonprofits
            GROUP BY parse_status ORDER BY n DESC
            """
        ).fetchall()
    finally:
        conn.close()

    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append("# Nonprofit Seed List — Coverage Report\n")
    lines.append(f"Generated: {now} UTC\n")
    lines.append(f"DB: `{db_path}`\n")
    lines.append("\n## Totals\n")
    lines.append(f"- EINs enumerated from sitemap: **{enumerated:,}**\n")
    lines.append(f"- Profiles successfully in `nonprofits`: **{fetched:,}**\n")
    lines.append(f"- `fetch_log` rows (all attempts): **{fetch_log_rows:,}**\n")
    lines.append(f"- Distinct URLs attempted: **{distinct_urls:,}**\n")
    lines.append(f"- Fetches ok: {ok:,}\n")
    lines.append(f"- Fetches rate-limited: {rate_limited:,}  "
                 f"(rate: {_pct(rate_limited, distinct_urls)})\n")
    lines.append(f"- Fetches challenged: {challenge:,}\n")
    lines.append(f"- Fetches not-found: {not_found:,}\n")

    lines.append("\n## Field population (within `nonprofits`)\n")
    lines.append(f"- `website_url`: {website:,} / {fetched:,} ({_pct(website, fetched)})\n")
    lines.append(f"- `rating_stars`: {rating:,} / {fetched:,} ({_pct(rating, fetched)})\n")
    lines.append(f"- `total_revenue`: {revenue:,} / {fetched:,} ({_pct(revenue, fetched)})\n")
    lines.append(f"- `state`: {state:,} / {fetched:,} ({_pct(state, fetched)})\n")
    lines.append(f"- `mission`: {mission:,} / {fetched:,} ({_pct(mission, fetched)})\n")
    lines.append("\nField-population below 50% triggers a manual review (parser regression "
                 "vs. dataset-just-like-that).\n")

    lines.append("\n## Top 20 states\n\n| State | Count |\n|---|---:|\n")
    for st, n in by_state:
        lines.append(f"| {st} | {n:,} |\n")

    lines.append("\n## Top NTEE majors\n\n| NTEE | Count |\n|---|---:|\n")
    for nt, n in by_ntee:
        lines.append(f"| {nt} | {n:,} |\n")

    lines.append("\n## Rating distribution\n\n| Stars | Count |\n|---|---:|\n")
    for rs, n in by_rating:
        lines.append(f"| {rs if rs is not None else 'unrated'} | {n:,} |\n")

    lines.append("\n## Parse status\n\n| Status | Count |\n|---|---:|\n")
    for ps, n in parse_status:
        lines.append(f"| {ps} | {n:,} |\n")

    return "".join(lines)


def write(db_path: Path | str = config.DB_PATH,
          out_path: Path | str | None = None) -> Path:
    """Generate report and write to `out_path` (default: ROOT/coverage_report.md)."""
    out_path = Path(out_path or config.ROOT / "coverage_report.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = generate(db_path)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(body)
    os.replace(tmp, out_path)
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass
    return out_path


if __name__ == "__main__":
    import sys
    path = write()
    print(f"wrote {path}", file=sys.stderr)

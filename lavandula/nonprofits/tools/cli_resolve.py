"""CLI-model resolver for nonprofit website URLs (Spec 0018 amendment).

Model-agnostic pipeline that calls gemini/codex/claude CLIs headlessly
to resolve nonprofit website URLs. Each model searches the web and returns
structured JSON with the same fields as the Gemma pipeline.

Usage:
    python -m lavandula.nonprofits.tools.cli_resolve --state NY --resolver codex [OPTIONS]
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from lavandula.common.db import make_app_engine

log = logging.getLogger(__name__)

_SCHEMA = "lava_impact"

_RESOLVER_CONFIGS = {
    "codex": {
        "cmd": ["codex", "exec", "--full-auto", "--ephemeral"],
        "output_flag": "-o",
        "method": "codex-gpt54-v1",
    },
    "codex-mini": {
        "cmd": ["codex", "exec", "--full-auto", "--ephemeral", "-m", "gpt-5.4-mini"],
        "output_flag": "-o",
        "method": "codex-gpt54mini-v1",
    },
    "gemini": {
        "cmd": ["gemini", "-p"],
        "output_flag": None,
        "method": "gemini-flash-v1",
        "extra_args": ["--yolo", "--model", "gemini-2.5-flash", "-o", "text"],
    },
    "claude": {
        "cmd": ["claude", "-p"],
        "output_flag": None,
        "method": "claude-opus-v1",
        "extra_args": ["--output-format", "text"],
    },
}

_PROMPT_TEMPLATE = (
    "Find the official website for the nonprofit '{name}' located in {city}, {state}. "
    "Search the web. Do NOT return directory listings, aggregator sites, social media pages, "
    "or sites belonging to a different organization with a similar name. "
    "Return ONLY a JSON object (no markdown, no explanation) with exactly these fields:\n"
    '{{"website_url": "the chosen URL or null", '
    '"website_candidates": ["url1", "url2", ...], '
    '"resolver_status": "resolved or unresolved", '
    '"resolver_confidence": 0.0 to 1.0, '
    '"resolver_reason": "brief explanation of why this URL is or is not the official site"}}\n'
    "website_candidates must list ALL URLs you considered, including ones you rejected."
)


def _build_prompt(org: dict) -> str:
    return _PROMPT_TEMPLATE.format(
        name=org["name"],
        city=org["city"],
        state=org["state"],
    )


def _run_codex(prompt: str, config: dict, timeout: int) -> dict | None:
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
        outpath = f.name

    cmd = config["cmd"] + [config["output_flag"], outpath, prompt]
    try:
        subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        result_text = Path(outpath).read_text().strip()
    except subprocess.TimeoutExpired:
        log.warning("Codex timed out")
        return None
    except Exception as exc:
        log.warning("Codex error: %s", exc)
        return None
    finally:
        Path(outpath).unlink(missing_ok=True)

    return _parse_json(result_text)


def _run_gemini(prompt: str, config: dict, timeout: int) -> dict | None:
    cmd = config["cmd"] + config.get("extra_args", []) + [prompt]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning("Gemini timed out")
        return None
    except Exception as exc:
        log.warning("Gemini error: %s", exc)
        return None

    return _parse_json(output)


def _run_claude(prompt: str, config: dict, timeout: int) -> dict | None:
    cmd = config["cmd"] + config.get("extra_args", []) + [prompt]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning("Claude timed out")
        return None
    except Exception as exc:
        log.warning("Claude error: %s", exc)
        return None

    return _parse_json(output)


_RUNNERS = {
    "codex": _run_codex,
    "codex-mini": _run_codex,
    "gemini": _run_gemini,
    "claude": _run_claude,
}


_timestamp_col_exists: bool | None = None


def _check_timestamp_column(engine) -> bool:
    global _timestamp_col_exists
    if _timestamp_col_exists is not None:
        return _timestamp_col_exists
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'lava_impact' AND table_name = 'nonprofits_seed' "
            "AND column_name = 'resolver_updated_at'"
        )).fetchone()
        _timestamp_col_exists = row is not None
    return _timestamp_col_exists


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("```"):
            continue
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    try:
        clean = text.strip()
        if clean.startswith("```"):
            clean = "\n".join(
                l for l in clean.splitlines()
                if not l.strip().startswith("```")
            )
        return json.loads(clean)
    except json.JSONDecodeError:
        log.warning("Failed to parse JSON from output: %s", text[:200])
        return None


def _write_result(engine, ein: str, result: dict, method: str) -> None:
    url = result.get("website_url")
    status = result.get("resolver_status", "unresolved")
    confidence = result.get("resolver_confidence", 0.0)
    reason = result.get("resolver_reason", "")
    candidates = result.get("website_candidates", [])

    if status not in ("resolved", "unresolved", "ambiguous"):
        status = "unresolved"
    if url and status == "resolved":
        pass
    elif url is None or url == "null":
        url = None
        if status == "resolved":
            status = "unresolved"

    has_timestamp = _check_timestamp_column(engine)

    candidates_json = json.dumps(candidates) if candidates else None

    if has_timestamp:
        sql = text(f"""
            UPDATE {_SCHEMA}.nonprofits_seed
            SET website_url = :url,
                website_candidates_json = :candidates,
                resolver_status = :status,
                resolver_confidence = :confidence,
                resolver_method = :method,
                resolver_reason = :reason,
                resolver_updated_at = :updated_at
            WHERE ein = :ein
        """)
    else:
        sql = text(f"""
            UPDATE {_SCHEMA}.nonprofits_seed
            SET website_url = :url,
                website_candidates_json = :candidates,
                resolver_status = :status,
                resolver_confidence = :confidence,
                resolver_method = :method,
                resolver_reason = :reason
            WHERE ein = :ein
        """)

    params = {
        "ein": ein,
        "url": url,
        "candidates": candidates_json,
        "status": status,
        "confidence": confidence,
        "method": method,
        "reason": reason[:500] if reason else None,
    }
    if has_timestamp:
        params["updated_at"] = datetime.now(timezone.utc)

    with engine.begin() as conn:
        conn.execute(sql, params)


def load_unresolved_orgs(engine, *, state, limit=None, fresh_only=False):
    sql = (
        f"SELECT ein, name, address, city, state, zipcode "
        f"FROM {_SCHEMA}.nonprofits_seed "
        f"WHERE state=:state"
    )
    if fresh_only:
        sql += " AND resolver_status IS NULL"
    else:
        sql += " AND (resolver_status IS NULL OR resolver_status = 'unresolved')"

    sql += " ORDER BY ein"
    if limit:
        sql += " LIMIT :lim"

    params: dict = {"state": state}
    if limit:
        params["lim"] = limit

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    return [
        {"ein": r[0], "name": r[1] or "", "address": r[2] or "",
         "city": r[3] or "", "state": r[4] or "", "zipcode": r[5] or ""}
        for r in rows
    ]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cli_resolve",
        description="Resolve nonprofit URLs via CLI model (codex/gemini/claude).",
    )
    p.add_argument("--state", required=True, help="Filter to orgs in this state")
    p.add_argument("--resolver", required=True, choices=list(_RESOLVER_CONFIGS.keys()),
                   help="Which CLI model to use")
    p.add_argument("--limit", type=int, default=0, help="Max orgs (0 = no limit)")
    p.add_argument("--fresh-only", action="store_true",
                   help="Only process orgs with resolver_status IS NULL")
    p.add_argument("--timeout", type=int, default=120,
                   help="Timeout per org in seconds")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Delay between orgs in seconds (rate limiting)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print prompts without calling model or writing DB")
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _build_parser().parse_args(argv)

    config = _RESOLVER_CONFIGS[args.resolver]
    runner = _RUNNERS[args.resolver]
    method = config["method"]

    engine = make_app_engine()

    try:
        limit = args.limit if args.limit > 0 else None
        orgs = load_unresolved_orgs(
            engine, state=args.state, limit=limit, fresh_only=args.fresh_only,
        )
        log.info("Loaded %d orgs to process with %s", len(orgs), args.resolver)

        if not orgs:
            print("No orgs to process.")
            return

        resolved = 0
        unresolved = 0
        errors = 0
        t_start = time.monotonic()

        for i, org in enumerate(orgs, 1):
            ein = org["ein"]
            name = org["name"]
            city = org["city"]
            state = org["state"]

            prompt = _build_prompt(org)

            if args.dry_run:
                print(f"[{i}/{len(orgs)}] {name}, {city}, {state}")
                print(f"  Prompt: {prompt[:120]}...")
                continue

            log.info("[%d/%d] %s, %s, %s (ein=%s)", i, len(orgs), name, city, state, ein)

            result = runner(prompt, config, args.timeout)

            if result is None:
                log.warning("No result for ein=%s, marking unresolved", ein)
                _write_result(engine, ein, {
                    "resolver_status": "unresolved",
                    "resolver_confidence": 0.0,
                    "resolver_reason": "cli_timeout_or_parse_error",
                }, method)
                errors += 1
            else:
                _write_result(engine, ein, result, method)
                status = result.get("resolver_status", "unresolved")
                url = result.get("website_url")
                conf = result.get("resolver_confidence", 0)
                if status == "resolved":
                    resolved += 1
                    log.info("  RESOLVED: %s (conf=%.2f)", url, conf)
                else:
                    unresolved += 1
                    log.info("  UNRESOLVED (conf=%.2f): %s",
                             conf, result.get("resolver_reason", "")[:80])

            if i < len(orgs) and args.delay > 0:
                time.sleep(args.delay)

        wall_time = time.monotonic() - t_start

        print(f"\n--- {args.resolver.title()} Resolver Summary ---")
        print(f"Wall time: {wall_time:.1f}s")
        print(f"Resolved: {resolved}")
        print(f"Unresolved: {unresolved}")
        print(f"Errors: {errors}")
        total = resolved + unresolved + errors
        if wall_time > 0 and total > 0:
            print(f"Rate: {total / wall_time * 60:.1f} orgs/minute")

    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

"""Agent-based URL resolver for nonprofit seeds (Spec 0008).

Batches orgs from seeds.db, spawns Claude Code sub-agents in parallel
(each restricted to WebSearch+WebFetch), and ingests agent-produced
URLs back into the DB.

Usage:
    python -m lavandula.nonprofits.tools.batch_resolve --db data/seeds.db [OPTIONS]

Resume:
    python -m lavandula.nonprofits.tools.batch_resolve --resume <run-dir>
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import json
import logging
import os
import secrets
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from lavandula.nonprofits.agent_runner import (
    AGENT_DISALLOWED_TOOLS,
    AGENT_MAX_OUTPUT_BYTES,
    AgentInvocation,
    AgentRunner,
    ClaudeCodeAgentRunner,
    PROMPT_VERSION,
)
from lavandula.nonprofits.batch_manifest import (
    BatchState,
    FingerprintMismatch,
    ManifestCorruptError,
    RunManifest,
    RunnerLockedError,
    compute_fingerprint,
    fingerprint_diff,
    locked,
)
from lavandula.nonprofits.tools.seed_enumerate import _EIN_RE

log = logging.getLogger(__name__)

MAX_BATCH_SIZE = 50
MAX_PARALLELISM = 4
DEFAULT_MAX_ORGS = 500
DEFAULT_BATCH_SIZE = 50
DEFAULT_PARALLELISM = 2
DEFAULT_TIMEOUT_PER_ORG = 30

CONFIDENCE_LEVELS = ("high", "medium", "low", "none")

MAX_LINE_BYTES = 16_384
MAX_URL_LENGTH = 2048
MAX_REASONING_CHARS = 500

UPDATE_SQL = text("""
UPDATE nonprofits_seed
   SET website_url = :url,
       resolver_status = :status,
       resolver_confidence = :confidence,
       resolver_method = :method,
       resolver_reason = :reason,
       website_candidates_json = :candidates_json
 WHERE ein = :ein
""")


# ── argv ────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="batch_resolve",
        description="Agent-based URL resolver for nonprofit seeds.",
    )
    ap.add_argument("--db", help="Path to seeds.db (required unless --resume)")
    ap.add_argument("--resume", help="Resume a prior run directory")
    ap.add_argument("--state", help="State filter, comma-separated (e.g. NY,MA)")
    ap.add_argument("--ntee-major", dest="ntee_major",
                    help="NTEE major letter filter, comma-separated")
    ap.add_argument("--revenue-min", dest="revenue_min", type=int)
    ap.add_argument("--revenue-max", dest="revenue_max", type=int)
    ap.add_argument("--max-orgs", dest="max_orgs", type=int,
                    default=DEFAULT_MAX_ORGS)
    ap.add_argument("--batch-size", dest="batch_size", type=int,
                    default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--parallelism", type=int, default=DEFAULT_PARALLELISM)
    ap.add_argument("--model", choices=("haiku", "opus", "sonnet"),
                    default="haiku")
    ap.add_argument("--re-resolve", dest="re_resolve", action="store_true")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--results-dir", dest="results_dir")
    ap.add_argument("--agent-timeout-per-org", dest="agent_timeout_per_org",
                    type=int, default=DEFAULT_TIMEOUT_PER_ORG)
    ap.add_argument("--yes", action="store_true",
                    help="Skip confirmation prompt")
    return ap


def _validate_args(parser: argparse.ArgumentParser,
                   args: argparse.Namespace) -> None:
    if not args.resume and not args.db:
        parser.error("--db is required unless --resume is given")
    if args.batch_size < 1 or args.batch_size > MAX_BATCH_SIZE:
        parser.error(f"batch-size must be 1..{MAX_BATCH_SIZE}")
    if args.parallelism < 1 or args.parallelism > MAX_PARALLELISM:
        parser.error(f"parallelism must be 1..{MAX_PARALLELISM}")
    if args.max_orgs < 1:
        parser.error("max-orgs must be >= 1")
    if args.agent_timeout_per_org < 1:
        parser.error("agent-timeout-per-org must be >= 1")


def _parse_csv(v: str | None) -> list[str] | None:
    if not v:
        return None
    out = [x.strip().upper() for x in v.split(",") if x.strip()]
    return out or None


def _normalize_filters(args: argparse.Namespace) -> None:
    args.state = _parse_csv(args.state)
    args.ntee_major = _parse_csv(args.ntee_major)


# ── work selection ──────────────────────────────────────────────────────────

SELECT_SQL_TMPL = """
SELECT ein, name, address, city, state, zipcode, ntee_code
  FROM nonprofits_seed
 WHERE 1=1
 {state_clause}
 {ntee_clause}
 {rev_min_clause}
 {rev_max_clause}
 {resolved_clause}
 ORDER BY ein ASC
 LIMIT :limit
"""


def _select_orgs(engine: Engine, args: argparse.Namespace) -> list[dict]:
    params: dict[str, Any] = {"limit": args.max_orgs}
    state_clause = ""
    ntee_clause = ""
    rev_min_clause = ""
    rev_max_clause = ""
    resolved_clause = ""
    if args.state:
        placeholders = ",".join(f":st{i}" for i in range(len(args.state)))
        state_clause = f"AND state IN ({placeholders})"
        for i, v in enumerate(args.state):
            params[f"st{i}"] = v
    if args.ntee_major:
        placeholders = ",".join(f":nt{i}" for i in range(len(args.ntee_major)))
        ntee_clause = f"AND substr(ntee_code,1,1) IN ({placeholders})"
        for i, v in enumerate(args.ntee_major):
            params[f"nt{i}"] = v
    if args.revenue_min is not None:
        rev_min_clause = "AND revenue >= :rev_min"
        params["rev_min"] = args.revenue_min
    if args.revenue_max is not None:
        rev_max_clause = "AND revenue <= :rev_max"
        params["rev_max"] = args.revenue_max
    if not args.re_resolve:
        resolved_clause = (
            "AND (resolver_status IS NULL OR resolver_status != 'resolved')"
        )
    sql = SELECT_SQL_TMPL.format(
        state_clause=state_clause,
        ntee_clause=ntee_clause,
        rev_min_clause=rev_min_clause,
        rev_max_clause=rev_max_clause,
        resolved_clause=resolved_clause,
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]


def _count_eligible(engine: Engine, args: argparse.Namespace) -> int:
    """Count rows matching filters, ignoring the --max-orgs LIMIT."""
    saved = args.max_orgs
    try:
        args.max_orgs = 10**9
        return len(_select_orgs(engine, args))
    finally:
        args.max_orgs = saved


# ── batch file generation ───────────────────────────────────────────────────

def _write_batch_input(path: Path, orgs: list[dict]) -> None:
    """Write batch-NNN-input.jsonl via json.dumps per line (AC23)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for org in orgs:
            # Only the fields the prompt uses. json.dumps escapes all the
            # nasties — newlines, quotes, control chars — per AC23.
            record = {
                "ein": org["ein"],
                "name": org.get("name") or "",
                "address": org.get("address") or "",
                "city": org.get("city") or "",
                "state": org.get("state") or "",
                "zipcode": org.get("zipcode") or "",
                "ntee_code": org.get("ntee_code") or "",
            }
            f.write(json.dumps(record, ensure_ascii=True) + "\n")


def _chunk(orgs: list[dict], size: int) -> list[list[dict]]:
    return [orgs[i:i + size] for i in range(0, len(orgs), size)]


# ── run directory helpers ───────────────────────────────────────────────────

def _make_run_dir(base: Path) -> Path:
    ts = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H-%M-%S")
    run_id = secrets.token_hex(3)  # 6 hex chars
    d = base / f"run-{ts}-{run_id}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def _default_results_dir(db_path: str) -> Path:
    return Path(db_path).resolve().parent / "agent-results"


def _run_id_from_dir(run_dir: Path) -> str:
    # run-YYYY-MM-DDTHH-MM-SS-XXXXXX → last 6 chars
    return run_dir.name.split("-")[-1]


# ── budget / plan ───────────────────────────────────────────────────────────

def _print_plan(args: argparse.Namespace, n_orgs: int, truncated_from: int | None,
                file=None) -> None:
    if file is None:
        file = sys.stderr
    n_batches = (n_orgs + args.batch_size - 1) // args.batch_size if n_orgs else 0
    est_tokens = n_orgs * 8_000  # ~8K avg per org per TX 100 baseline
    est_wall_min = (n_batches / max(args.parallelism, 1)) * 20
    lines = [
        "Batch plan:",
        f"  Orgs to process: {n_orgs}",
        f"  Batches: {n_batches} (of up to {args.batch_size} orgs each)",
        f"  Parallelism: {args.parallelism}",
        f"  Model: {args.model}",
        f"  Estimated tokens: ~{est_tokens:,} "
        f"(~8K/org based on TX 100 run avg)",
        f"  Estimated wall time: ~{est_wall_min:.0f} min",
    ]
    if truncated_from is not None:
        lines.append(
            f"  (truncated from {truncated_from} eligible orgs by "
            f"--max-orgs {args.max_orgs})"
        )
    print("\n".join(lines), file=file)


def _confirm_interactively() -> bool:
    """Return True only on y/Y/yes/YES. Any other input / EOF → False."""
    try:
        sys.stdout.write("Proceed? [y/N] ")
        sys.stdout.flush()
        raw = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return False
    if not raw:
        return False
    return raw.strip().lower() in {"y", "yes"}


# ── ingestion ───────────────────────────────────────────────────────────────

def _confidence_to_status(conf: str) -> tuple[str, float]:
    return {
        "high": ("resolved", 0.9),
        "medium": ("resolved", 0.6),
        "low": ("ambiguous", 0.3),
        "none": ("unresolved", 0.0),
    }[conf]


def _validate_url(url: Any) -> bool:
    if url is None:
        return True
    if not isinstance(url, str):
        return False
    if len(url) > MAX_URL_LENGTH:
        return False
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def parse_output_file(paths: Iterable[Path], batch_input_eins: set[str],
                      warn: Any = None) -> dict[str, dict]:
    """Parse one or more batch-output JSONL files.

    Returns a dict mapping ein → last-seen valid row (AC19: last-write-wins).
    Invalid lines are skipped with a warning and never raise.
    """
    rows: dict[str, dict] = {}
    warn_fn = warn or (lambda msg, **kw: log.warning(msg, extra=kw))
    for path in paths:
        if not Path(path).exists():
            continue
        try:
            raw = Path(path).read_text()
        except OSError as exc:
            warn_fn(f"read error on {path}: {exc}")
            continue
        for line_num, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            if len(line.encode("utf-8", errors="replace")) > MAX_LINE_BYTES:
                warn_fn(f"line too long in {path.name}:{line_num}; skipping")
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                warn_fn(f"malformed JSON at {path.name}:{line_num}: {exc}")
                continue
            if not isinstance(obj, dict):
                warn_fn(f"non-object at {path.name}:{line_num}")
                continue
            ein = obj.get("ein")
            if not isinstance(ein, str) or not _EIN_RE.fullmatch(ein):
                warn_fn(f"invalid ein at {path.name}:{line_num}: {ein!r}")
                continue
            if ein not in batch_input_eins:
                warn_fn(f"ein {ein} not in batch input at "
                        f"{path.name}:{line_num}; skipping")
                continue
            conf = obj.get("confidence")
            if conf not in CONFIDENCE_LEVELS:
                warn_fn(f"invalid confidence {conf!r} at "
                        f"{path.name}:{line_num}")
                continue
            url = obj.get("url")
            if conf == "none":
                url = None
            elif not _validate_url(url):
                warn_fn(f"invalid url at {path.name}:{line_num}")
                continue
            reasoning = obj.get("reasoning") or ""
            if not isinstance(reasoning, str):
                reasoning = str(reasoning)
            if len(reasoning) > MAX_REASONING_CHARS:
                reasoning = reasoning[:MAX_REASONING_CHARS - 3] + "..."
            if ein in rows:
                log.info("duplicate ein %s in %s — last line wins",
                         ein, path.name)
            rows[ein] = {
                "ein": ein,
                "url": url,
                "confidence": conf,
                "reasoning": reasoning,
            }
    return rows


def ingest_rows(engine: Engine, rows: dict[str, dict], *,
                model: str, re_resolve: bool) -> tuple[int, int]:
    """Ingest parsed rows in a single transaction. Returns (written, skipped)."""
    if not rows:
        return (0, 0)
    written = 0
    skipped = 0
    method = f"claude-{model}-agent-v{PROMPT_VERSION}"
    with engine.begin() as conn:
        if not re_resolve:
            eins = list(rows.keys())
            # Chunk IN-clause to stay under SQLite's 1000-param cap.
            already: set[str] = set()
            for i in range(0, len(eins), 500):
                chunk = eins[i:i + 500]
                placeholders = ",".join(f":e{j}" for j in range(len(chunk)))
                params = {f"e{j}": v for j, v in enumerate(chunk)}
                q = text(
                    "SELECT ein FROM nonprofits_seed "
                    f"WHERE ein IN ({placeholders}) "
                    "AND resolver_status = 'resolved'"
                )
                already.update(r[0] for r in conn.execute(q, params))
            for ein in already:
                skipped += 1
        else:
            already = set()

        for ein, row in rows.items():
            if ein in already:
                continue
            status, confidence = _confidence_to_status(row["confidence"])
            candidates = [{
                "url": row["url"],
                "confidence": row["confidence"],
                "reasoning": row["reasoning"],
            }]
            result = conn.execute(UPDATE_SQL, {
                "ein": ein,
                "url": row["url"],
                "status": status,
                "confidence": confidence,
                "method": method,
                "reason": row["reasoning"],
                "candidates_json": json.dumps(candidates, ensure_ascii=True),
            })
            if result.rowcount > 0:
                written += 1
            else:
                skipped += 1
    return written, skipped


# ── run.log event stream ────────────────────────────────────────────────────

class EventLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Touch
        self.path.touch(exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        payload = {
            "ts": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            "event": event,
            **fields,
        }
        with open(self.path, "a") as f:
            f.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")


# ── orchestration ───────────────────────────────────────────────────────────

def _build_initial_manifest(args: argparse.Namespace,
                            orgs: list[dict],
                            run_id: str) -> RunManifest:
    batches: list[BatchState] = []
    for i, chunk in enumerate(_chunk(orgs, args.batch_size)):
        batches.append(BatchState(
            id=i,
            ein_first=chunk[0]["ein"],
            ein_last=chunk[-1]["ein"],
            input_count=len(chunk),
            completed_count=0,
            state="pending",
        ))
    return RunManifest(
        run_id=run_id,
        started_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        fingerprint=compute_fingerprint(args, PROMPT_VERSION),
        args=_manifest_args(args),
        total_orgs=len(orgs),
        batches=batches,
    )


def _manifest_args(args: argparse.Namespace) -> dict:
    return {
        "db_path_canonical": os.path.realpath(args.db) if args.db else "",
        "state": sorted(args.state or []),
        "ntee_major": sorted(args.ntee_major or []),
        "revenue_min": args.revenue_min,
        "revenue_max": args.revenue_max,
        "max_orgs": args.max_orgs,
        "batch_size": args.batch_size,
        "model": args.model,
        "re_resolve": bool(args.re_resolve),
        "prompt_version": PROMPT_VERSION,
        "parallelism": args.parallelism,
        "agent_timeout_per_org": args.agent_timeout_per_org,
    }


def _summarize(manifest: RunManifest, *, started_monotonic: float,
               ingest_stats: dict) -> dict:
    conf_counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
    status_counts = {"resolved": 0, "ambiguous": 0, "unresolved": 0}
    for row in ingest_stats.get("rows_by_conf", []):
        conf_counts[row] = conf_counts.get(row, 0) + 1
        status, _ = _confidence_to_status(row)
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "run_id": manifest.run_id,
        "started_at": manifest.started_at,
        "ended_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "wall_time_sec": round(time.monotonic() - started_monotonic, 2),
        "total_orgs": manifest.total_orgs,
        "batches_complete": sum(1 for b in manifest.batches
                                if b.state in ("complete", "ingested")),
        "batches_failed": sum(1 for b in manifest.batches
                              if b.state == "failed"),
        "batches_partial": sum(1 for b in manifest.batches
                               if b.state == "partial"),
        "agent_calls_attempted": ingest_stats.get("agent_calls_attempted", 0),
        "agent_calls_succeeded": ingest_stats.get("agent_calls_succeeded", 0),
        "confidence_breakdown": conf_counts,
        "resolver_status_breakdown": status_counts,
        "rows_written": ingest_stats.get("rows_written", 0),
        "rows_skipped": ingest_stats.get("rows_skipped", 0),
    }


def _load_batch_input_eins(run_dir: Path, batch_id: int) -> set[str]:
    path = run_dir / f"batch-{batch_id:03d}-input.jsonl"
    if not path.exists():
        return set()
    eins: set[str] = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ein = obj.get("ein")
        if isinstance(ein, str) and _EIN_RE.fullmatch(ein):
            eins.add(ein)
    return eins


def _batch_output_paths(run_dir: Path, batch_id: int,
                        continuation_count: int) -> list[Path]:
    paths = [run_dir / f"batch-{batch_id:03d}-output.jsonl"]
    for k in range(1, continuation_count + 1):
        paths.append(
            run_dir / f"batch-{batch_id:03d}-continuation-{k}-output.jsonl"
        )
    return paths


def _input_for_batch(run_dir: Path, batch: BatchState,
                     orgs_by_ein: dict[str, dict] | None,
                     remaining_eins: set[str] | None = None) -> Path:
    """Return the input path to feed to the agent for this attempt.

    For a fresh batch: `batch-NNN-input.jsonl` (already written).
    For a partial resume: `batch-NNN-continuation-K-input.jsonl`.
    """
    if remaining_eins is None:
        return run_dir / f"batch-{batch.id:03d}-input.jsonl"
    k = batch.continuation_count + 1
    cont_path = run_dir / f"batch-{batch.id:03d}-continuation-{k}-input.jsonl"
    assert orgs_by_ein is not None
    orgs = [orgs_by_ein[e] for e in sorted(remaining_eins) if e in orgs_by_ein]
    _write_batch_input(cont_path, orgs)
    return cont_path


def _output_for_attempt(run_dir: Path, batch: BatchState,
                        is_continuation: bool) -> Path:
    if not is_continuation:
        return run_dir / f"batch-{batch.id:03d}-output.jsonl"
    k = batch.continuation_count + 1
    return run_dir / f"batch-{batch.id:03d}-continuation-{k}-output.jsonl"


# ── main orchestration flow ─────────────────────────────────────────────────

def run(args: argparse.Namespace, *,
        agent_runner_factory: Any = None,
        ) -> int:
    """Core orchestration. Returns an exit code."""
    _normalize_filters(args)

    # ── resume path ──────────────────────────────────────────────────────
    if args.resume:
        run_dir = Path(args.resume).resolve()
        manifest_path = run_dir / "RUN_MANIFEST.json"
        try:
            manifest = RunManifest.load(manifest_path)
        except ManifestCorruptError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        # Rehydrate args from manifest for db / filters so fingerprint
        # comparison reflects the user's new invocation. The user must
        # re-supply filters that match; any mismatch aborts with exit 2.
        # Inject canonical db path for compute_fingerprint.
        if not args.db:
            args.db = manifest.args.get("db_path_canonical", "")

        diff = fingerprint_diff(manifest.args, args, PROMPT_VERSION)
        if diff:
            print("cannot resume run: fingerprint differs", file=sys.stderr)
            print("  changed fields:", file=sys.stderr)
            for name, old, new in diff:
                print(f"    {name} (manifest={old!r} vs current={new!r})",
                      file=sys.stderr)
            return 2

        if all(b.state == "ingested" for b in manifest.batches):
            print("all batches already ingested; nothing to do.",
                  file=sys.stderr)
            return 0
    else:
        if not args.db:
            print("error: --db is required", file=sys.stderr)
            return 2
        results_dir = Path(args.results_dir) if args.results_dir \
            else _default_results_dir(args.db)
        results_dir.mkdir(parents=True, exist_ok=True)
        run_dir = _make_run_dir(results_dir)
        manifest_path = run_dir / "RUN_MANIFEST.json"
        manifest = None  # built after query

    # ── engine + query (for new runs) ────────────────────────────────────
    engine = create_engine(f"sqlite:///{args.db}")

    try:
        if manifest is None:
            # Count eligible + truncate.
            total_eligible = _count_eligible(engine, args)
            orgs = _select_orgs(engine, args)
            truncated_from = (
                total_eligible if total_eligible > len(orgs) else None
            )
            if truncated_from:
                log.warning(
                    "max-orgs truncation: eligible=%d selected=%d (truncated=%d)",
                    total_eligible, len(orgs),
                    total_eligible - len(orgs),
                )
                print(
                    f"warning: {truncated_from - len(orgs)} eligible orgs "
                    f"truncated by --max-orgs {args.max_orgs}",
                    file=sys.stderr,
                )
            _print_plan(args, len(orgs), truncated_from)
        else:
            # Resumed: rebuild orgs from existing input files so we can
            # regenerate continuations.
            orgs = _rebuild_orgs_from_manifest(run_dir, manifest)
            _print_plan(args, len(orgs), None)

        # ── dry run ───────────────────────────────────────────────────────
        if args.dry_run:
            # On dry-run, do not acquire the lock, do not write manifest.
            print("dry-run: no agents spawned, no DB writes.", file=sys.stderr)
            return 0

        # ── confirmation ─────────────────────────────────────────────────
        if not args.yes:
            if not sys.stdin.isatty():
                print(
                    "refusing to run without confirmation in non-interactive "
                    "mode;\npass --yes or run in a terminal",
                    file=sys.stderr,
                )
                return 2
            if not _confirm_interactively():
                print("aborted.", file=sys.stderr)
                return 1

        # ── acquire lock + write initial manifest ────────────────────────
        try:
            lock_ctx = locked(manifest_path)
            lock_fh = lock_ctx.__enter__()
        except RunnerLockedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        try:
            if manifest is None:
                run_id = _run_id_from_dir(run_dir)
                manifest = _build_initial_manifest(args, orgs, run_id)
                # Write the batch input files up front (json.dumps per line).
                for i, chunk in enumerate(_chunk(orgs, args.batch_size)):
                    _write_batch_input(
                        run_dir / f"batch-{i:03d}-input.jsonl", chunk,
                    )
                manifest.save(manifest_path)

            event_log = EventLog(run_dir / "run.log")
            event_log.emit(
                "run_start",
                run_id=manifest.run_id,
                started_at=manifest.started_at,
                total_orgs=manifest.total_orgs,
                batches=len(manifest.batches),
                args=manifest.args,
            )

            return _drive_batches(
                args=args,
                engine=engine,
                run_dir=run_dir,
                manifest=manifest,
                manifest_path=manifest_path,
                orgs_by_ein={o["ein"]: o for o in orgs},
                event_log=event_log,
                agent_runner_factory=agent_runner_factory,
            )
        finally:
            try:
                lock_ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
    finally:
        engine.dispose()


def _rebuild_orgs_from_manifest(run_dir: Path,
                                manifest: RunManifest) -> list[dict]:
    orgs: list[dict] = []
    for batch in manifest.batches:
        path = run_dir / f"batch-{batch.id:03d}-input.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                orgs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return orgs


def _drive_batches(*, args: argparse.Namespace, engine: Engine,
                   run_dir: Path, manifest: RunManifest,
                   manifest_path: Path,
                   orgs_by_ein: dict[str, dict],
                   event_log: EventLog,
                   agent_runner_factory: Any) -> int:
    started_mono = time.monotonic()
    ingest_stats = {
        "agent_calls_attempted": 0,
        "agent_calls_succeeded": 0,
        "rows_written": 0,
        "rows_skipped": 0,
        "rows_by_conf": [],
    }

    if agent_runner_factory is None:
        def agent_runner_factory() -> AgentRunner:
            return ClaudeCodeAgentRunner()

    # Build invocations for each batch that still needs work.
    todo: list[tuple[BatchState, AgentInvocation, bool]] = []
    tag_uuid = uuid.uuid4().hex
    for batch in manifest.batches:
        if batch.state == "ingested":
            continue
        if batch.state == "complete":
            # Already ran but not ingested — fall through to ingestion below.
            continue
        if batch.state == "partial":
            input_eins = _load_batch_input_eins(run_dir, batch.id)
            existing_rows = parse_output_file(
                _batch_output_paths(run_dir, batch.id, batch.continuation_count),
                input_eins,
            )
            remaining = input_eins - set(existing_rows.keys())
            if not remaining:
                batch.state = "complete"
                batch.completed_count = len(existing_rows)
                manifest.save(manifest_path)
                continue
            input_path = _input_for_batch(
                run_dir, batch, orgs_by_ein, remaining_eins=remaining,
            )
            output_path = _output_for_attempt(run_dir, batch, is_continuation=True)
            is_cont = True
        else:
            input_path = run_dir / f"batch-{batch.id:03d}-input.jsonl"
            output_path = run_dir / f"batch-{batch.id:03d}-output.jsonl"
            is_cont = False

        timeout_sec = args.batch_size * args.agent_timeout_per_org + 120
        inv = AgentInvocation(
            batch_id=batch.id,
            input_path=input_path,
            output_path=output_path,
            model=args.model,
            timeout_sec=timeout_sec,
            max_output_bytes=AGENT_MAX_OUTPUT_BYTES,
            tag_uuid=tag_uuid,
        )
        todo.append((batch, inv, is_cont))

    runner = agent_runner_factory()

    # ── parallel agent spawn, sequential ingestion on main thread ────────
    try:
        with futures.ThreadPoolExecutor(max_workers=args.parallelism) as pool:
            future_map: dict[futures.Future, tuple[BatchState, AgentInvocation, bool]] = {}
            for batch, inv, is_cont in todo:
                batch.state = "in_progress"
                manifest.save(manifest_path)
                event_log.emit(
                    "batch_submit", batch_id=batch.id,
                    ein_first=batch.ein_first, ein_last=batch.ein_last,
                    input_count=batch.input_count,
                )
                ingest_stats["agent_calls_attempted"] += 1
                fut = pool.submit(runner.run, inv)
                future_map[fut] = (batch, inv, is_cont)

            for fut in futures.as_completed(future_map):
                batch, inv, is_cont = future_map[fut]
                batch_started = time.monotonic()
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001
                    batch.state = "failed"
                    batch.error = f"{type(exc).__name__}: {exc}"
                    manifest.save(manifest_path)
                    event_log.emit("batch_complete", batch_id=batch.id,
                                   state="failed", error=batch.error)
                    continue

                if is_cont:
                    batch.continuation_count += 1

                # Normalize state on disk.
                batch.state = result.state
                batch.completed_count = result.completed_count
                if result.error:
                    batch.error = result.error
                manifest.save(manifest_path)

                if result.state in ("complete", "partial"):
                    ingest_stats["agent_calls_succeeded"] += 1

                event_log.emit(
                    "batch_complete",
                    batch_id=batch.id,
                    state=result.state,
                    completed_count=result.completed_count,
                    input_count=result.input_count,
                    duration_sec=round(time.monotonic() - batch_started, 2),
                    error=result.error,
                )

                # Ingest now (main thread).
                if result.state != "failed":
                    _ingest_single_batch(
                        batch=batch, engine=engine, run_dir=run_dir,
                        model=args.model, re_resolve=args.re_resolve,
                        event_log=event_log, ingest_stats=ingest_stats,
                        manifest=manifest, manifest_path=manifest_path,
                    )
    except KeyboardInterrupt:
        event_log.emit("run_interrupted")
        manifest.save(manifest_path)
        return 130

    # Ingest any batches that reached 'complete' state earlier without
    # being ingested (e.g. resumed run with complete-but-not-ingested).
    for batch in manifest.batches:
        if batch.state == "complete":
            _ingest_single_batch(
                batch=batch, engine=engine, run_dir=run_dir,
                model=args.model, re_resolve=args.re_resolve,
                event_log=event_log, ingest_stats=ingest_stats,
                manifest=manifest, manifest_path=manifest_path,
            )

    summary = _summarize(manifest, started_monotonic=started_mono,
                         ingest_stats=ingest_stats)
    manifest.summary = summary
    manifest.save(manifest_path)
    (run_dir / "run_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True)
    )
    event_log.emit("run_end", **summary)
    return 0


def _ingest_single_batch(*, batch: BatchState, engine: Engine,
                         run_dir: Path, model: str, re_resolve: bool,
                         event_log: EventLog, ingest_stats: dict,
                         manifest: RunManifest,
                         manifest_path: Path) -> None:
    input_eins = _load_batch_input_eins(run_dir, batch.id)
    out_paths = _batch_output_paths(run_dir, batch.id, batch.continuation_count)
    parsed = parse_output_file(
        out_paths, input_eins,
        warn=lambda msg, **kw: event_log.emit("warning", message=msg,
                                              batch_id=batch.id, context=kw),
    )
    try:
        written, skipped = ingest_rows(
            engine, parsed, model=model, re_resolve=re_resolve,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("ingestion failure in batch %d", batch.id)
        event_log.emit("warning", message=f"ingestion failed: {exc}",
                       batch_id=batch.id)
        # Leave batch.state as-is (complete/partial) — not ingested;
        # retry possible on resume.
        return

    ingest_stats["rows_written"] += written
    ingest_stats["rows_skipped"] += skipped
    ingest_stats["rows_by_conf"].extend(r["confidence"] for r in parsed.values())

    batch.state = "ingested"
    manifest.save(manifest_path)
    event_log.emit("batch_ingested", batch_id=batch.id,
                   rows_written=written, rows_skipped=skipped)


# ── entrypoint ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    try:
        return run(args)
    except FingerprintMismatch as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RunnerLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

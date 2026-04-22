"""Run-state persistence for the agent batch runner (Spec 0008).

Pure Python. No DB, no network. The manifest is an on-disk JSON file at
`{run-dir}/RUN_MANIFEST.json`. All writes use the atomic tmp + rename
pattern; a concurrent-run advisory flock guards the run directory.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BATCH_STATES = {
    "pending",
    "in_progress",
    "complete",
    "ingested",
    "partial",
    "failed",
    "timeout",
}


class ManifestCorruptError(Exception):
    """Manifest file exists but cannot be decoded into a RunManifest."""


class RunnerLockedError(Exception):
    """Another runner holds the advisory flock on the manifest."""


class FingerprintMismatch(Exception):
    """Resumed run's current args do not match the persisted fingerprint."""


@dataclass
class BatchState:
    id: int
    ein_first: str
    ein_last: str
    input_count: int
    completed_count: int = 0
    state: str = "pending"
    continuation_count: int = 0
    error: str | None = None

    def __post_init__(self) -> None:
        if self.state not in BATCH_STATES:
            raise ManifestCorruptError(f"invalid batch state: {self.state!r}")


@dataclass
class RunManifest:
    run_id: str
    started_at: str
    fingerprint: str
    args: dict
    total_orgs: int
    batches: list[BatchState] = field(default_factory=list)
    summary: dict | None = None

    # ── persistence ──────────────────────────────────────────────────────
    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(asdict(self), indent=2, sort_keys=True)
        tmp.write_text(payload)
        os.rename(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "RunManifest":
        path = Path(path)
        try:
            raw = path.read_text()
        except FileNotFoundError as exc:
            raise ManifestCorruptError(f"manifest not found: {path}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManifestCorruptError(f"manifest not JSON: {path}: {exc}") from exc
        try:
            batches = [BatchState(**b) for b in data.get("batches", [])]
            return cls(
                run_id=data["run_id"],
                started_at=data["started_at"],
                fingerprint=data["fingerprint"],
                args=data.get("args", {}),
                total_orgs=int(data.get("total_orgs", 0)),
                batches=batches,
                summary=data.get("summary"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestCorruptError(f"manifest schema error: {exc}") from exc


# ── fingerprint ─────────────────────────────────────────────────────────────

def compute_fingerprint(args: Any, prompt_version: int) -> str:
    """Stable 16-char hex hash of the inputs that determine org selection."""
    def _norm(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return sorted(str(x) for x in v)
        return v

    payload = {
        "db_path_canonical": os.path.realpath(getattr(args, "db", "")),
        "state": _norm(getattr(args, "state", None)),
        "ntee_major": _norm(getattr(args, "ntee_major", None)),
        "revenue_min": getattr(args, "revenue_min", None),
        "revenue_max": getattr(args, "revenue_max", None),
        "max_orgs": getattr(args, "max_orgs", None),
        "batch_size": getattr(args, "batch_size", None),
        "model": getattr(args, "model", None),
        "re_resolve": bool(getattr(args, "re_resolve", False)),
        "prompt_version": prompt_version,
    }
    raw = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def fingerprint_diff(manifest_args: dict, current_args: Any,
                     prompt_version: int) -> list[tuple[str, Any, Any]]:
    """Return [(field, manifest_value, current_value)] for fields that differ."""
    out: list[tuple[str, Any, Any]] = []
    checks = [
        ("db_path_canonical",
         manifest_args.get("db_path_canonical"),
         os.path.realpath(getattr(current_args, "db", ""))),
        ("state",
         manifest_args.get("state"),
         sorted(getattr(current_args, "state", None) or [])),
        ("ntee_major",
         manifest_args.get("ntee_major"),
         sorted(getattr(current_args, "ntee_major", None) or [])),
        ("revenue_min", manifest_args.get("revenue_min"),
         getattr(current_args, "revenue_min", None)),
        ("revenue_max", manifest_args.get("revenue_max"),
         getattr(current_args, "revenue_max", None)),
        ("max_orgs", manifest_args.get("max_orgs"),
         getattr(current_args, "max_orgs", None)),
        ("batch_size", manifest_args.get("batch_size"),
         getattr(current_args, "batch_size", None)),
        ("model", manifest_args.get("model"),
         getattr(current_args, "model", None)),
        ("re_resolve", manifest_args.get("re_resolve"),
         bool(getattr(current_args, "re_resolve", False))),
        ("prompt_version", manifest_args.get("prompt_version"),
         prompt_version),
    ]
    for name, m, c in checks:
        if m != c:
            out.append((name, m, c))
    return out


# ── concurrent-run lock ─────────────────────────────────────────────────────

LOCK_FILENAME = "run.lock"


@contextlib.contextmanager
def locked(manifest_path: Path):
    """Advisory flock on a sentinel lock file for the duration of a run.

    The lock is held on `{run-dir}/run.lock` — NOT on the manifest itself.
    Manifest writes use atomic tmp + rename, which replaces the inode.
    Flocking the manifest would leave the held fd pointing at an orphan
    inode, allowing a concurrent runner to flock the new inode and
    silently defeat the guard (spec AC15).
    """
    manifest_path = Path(manifest_path)
    lock_path = manifest_path.parent / LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fh.close()
            raise RunnerLockedError(
                f"another runner is using {manifest_path.parent}; "
                "wait for it to finish or pass a different --results-dir"
            ) from exc
        yield fh
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()

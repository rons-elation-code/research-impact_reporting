"""HMAC-integrity checkpoint storage.

Stores (fetched EIN set, last successful EIN, counters) as JSON with an
HMAC-SHA256 using a per-installation secret at `DATA/.crawler.key`.
A MAC mismatch is treated identically to corruption: rename
`checkpoint.corrupt-{ts}.json` and start fresh.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config
from .logging_utils import sanitize


_CORRUPT_RETENTION = 5


@dataclass
class CheckpointState:
    started_at: str = ""
    last_ein: str | None = None
    fetched_count: int = 0
    failed_count: int = 0
    consecutive_403: int = 0
    consecutive_429: int = 0
    consecutive_long_retry_after: int = 0


class CheckpointError(RuntimeError):
    pass


def _load_or_create_key(key_path: Path) -> bytes:
    """Load or generate the per-install HMAC key; mode 0o600."""
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        try:
            return base64.b64decode(key_path.read_bytes())
        except Exception as exc:
            raise CheckpointError(
                f"crawler key at {key_path} is unreadable: {sanitize(str(exc))}"
            ) from exc
    key = secrets.token_bytes(32)
    tmp = key_path.with_suffix(".tmp")
    tmp.write_bytes(base64.b64encode(key))
    os.replace(tmp, key_path)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


def _hmac(body: bytes, key: bytes) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def save(
    state: CheckpointState,
    *,
    path: Path | None = None,
    key_path: Path | None = None,
) -> None:
    path = path or config.CHECKPOINT_PATH
    key_path = key_path or config.CRAWLER_KEY_PATH
    key = _load_or_create_key(key_path)
    payload = json.dumps(asdict(state), sort_keys=True).encode("utf-8")
    mac = _hmac(payload, key)
    doc = {"payload": asdict(state), "mac": mac}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load(
    *,
    path: Path | None = None,
    key_path: Path | None = None,
) -> CheckpointState:
    """Load checkpoint; on corrupt/MAC-mismatch, rotate and return fresh."""
    path = path or config.CHECKPOINT_PATH
    key_path = key_path or config.CRAWLER_KEY_PATH
    if not path.exists():
        return CheckpointState(started_at=_dt.datetime.now(_dt.timezone.utc).isoformat())
    try:
        raw = path.read_text()
        doc = json.loads(raw)
        payload = doc["payload"]
        mac = doc["mac"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        _rotate_corrupt(path, reason=f"decode: {sanitize(str(exc))}")
        return CheckpointState(started_at=_dt.datetime.now(_dt.timezone.utc).isoformat())

    key = _load_or_create_key(key_path)
    expected_mac = _hmac(
        json.dumps(payload, sort_keys=True).encode("utf-8"),
        key,
    )
    if not hmac.compare_digest(mac, expected_mac):
        _rotate_corrupt(path, reason="HMAC mismatch")
        return CheckpointState(started_at=_dt.datetime.now(_dt.timezone.utc).isoformat())

    return CheckpointState(**payload)


def _rotate_corrupt(path: Path, *, reason: str) -> None:
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.stem}.corrupt-{ts}.json")
    try:
        os.replace(path, target)
    except OSError:
        return
    _trim_corrupt_history(path.parent, stem=path.stem)


def _trim_corrupt_history(directory: Path, *, stem: str) -> None:
    files = sorted(
        (p for p in directory.glob(f"{stem}.corrupt-*.json")),
        key=lambda p: p.stat().st_mtime,
    )
    while len(files) > _CORRUPT_RETENTION:
        doomed = files.pop(0)
        try:
            doomed.unlink()
        except OSError:
            pass

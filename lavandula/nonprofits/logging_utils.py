"""Log-injection-safe sanitation helpers.

`sanitize` strips control characters (0x00-0x1F, 0x7F) and truncates to a
cap so that attacker-controlled bytes (Retry-After headers, CN HTML, error
strings) cannot forge log lines, terminal escapes, or fill disk.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_CONTROL_CHARS = {c: "" for c in range(0x20)}
_CONTROL_CHARS[0x7F] = ""
for c in (0x09, 0x0A):  # allow horizontal tab / newline only inside multi-line logs
    _CONTROL_CHARS.pop(c, None)

# For log writes specifically we strip newlines too — those are the attack.
_LOG_CONTROL_CHARS = {c: "" for c in range(0x20)}
_LOG_CONTROL_CHARS[0x7F] = ""

DEFAULT_MAX_LEN = 500


def sanitize(value: object, *, max_len: int = DEFAULT_MAX_LEN) -> str:
    """Strip control chars and clip to max_len.

    None → empty string. Non-strings are coerced with str(). Always returns a
    string safe to concatenate into a log line or SQLite TEXT column.
    """
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    s = s.translate(_LOG_CONTROL_CHARS)
    if len(s) > max_len:
        s = s[:max_len] + "...<truncated>"
    return s


def sanitize_exception(exc: BaseException, *, max_len: int = 2000) -> str:
    """Sanitize an exception message and strip operator home-dir paths.

    Avoids accidental disclosure of deployment layout if a HALT file is
    shared with a third party.
    """
    msg = f"{type(exc).__name__}: {exc}"
    home = str(Path.home())
    if home and home != "/":
        msg = msg.replace(home, "~")
    return sanitize(msg, max_len=max_len)


def setup_logging(logs_dir: Path, name: str = "crawler") -> logging.Logger:
    """Configure a RotatingFileHandler at 100 MB * 5 + stderr.

    Creates `logs_dir` with mode 0o700 if missing. Safe to call more than once.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(logs_dir, 0o700)
    except OSError:
        pass

    logger = logging.getLogger("lavandula.nonprofits")
    if getattr(logger, "_lavandula_configured", False):
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logfile = logs_dir / f"{name}.log"
    fh = logging.handlers.RotatingFileHandler(
        logfile, maxBytes=100 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # Attempt 0o600 on the log file.
    try:
        os.chmod(logfile, 0o600)
    except OSError:
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger._lavandula_configured = True  # type: ignore[attr-defined]
    return logger

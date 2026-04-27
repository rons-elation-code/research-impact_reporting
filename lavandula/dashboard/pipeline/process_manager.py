from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from .models import Job, PipelineProcess
from .orchestrator import LOG_DIR, PROJECT_ROOT, build_argv


def start_process(name: str, config_json: dict) -> PipelineProcess:
    """Start an ad-hoc pipeline process."""
    if Job.objects.filter(phase=name, status="running").exists():
        raise RuntimeError(f"A queued job is already running for phase {name}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    with transaction.atomic():
        proc_row, _ = PipelineProcess.objects.get_or_create(
            name=name, defaults={"status": "stopped"}
        )
        proc_row = PipelineProcess.objects.select_for_update().get(pk=proc_row.pk)

        if proc_row.status == "running":
            if proc_row.pid and _is_pid_alive_and_matches(proc_row.pid, name):
                raise RuntimeError(f"{name} is already running (PID {proc_row.pid})")
            proc_row.status = "stopped"
            proc_row.save(update_fields=["status"])

        argv = build_argv(name, config_json)
        ts = timezone.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOG_DIR / f"{name}_{ts}.log"

        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}

        try:
            log_fh = open(log_path, "w")
            child = subprocess.Popen(
                argv,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                env=env,
                preexec_fn=os.setpgrp,
            )
        except (OSError, FileNotFoundError) as exc:
            proc_row.status = "error"
            proc_row.error_message = f"Spawn error: {exc}"
            proc_row.save(update_fields=["status", "error_message"])
            raise RuntimeError(f"Failed to start {name}: {exc}") from exc

        proc_row.pid = child.pid
        proc_row.status = "running"
        proc_row.started_at = timezone.now()
        proc_row.last_heartbeat = timezone.now()
        proc_row.log_file = str(log_path)
        proc_row.config_json = config_json
        proc_row.error_message = None

        try:
            proc_row.save(update_fields=[
                "pid", "status", "started_at", "last_heartbeat",
                "log_file", "config_json", "error_message",
            ])
        except Exception:
            time.sleep(1)
            try:
                proc_row.save(update_fields=[
                    "pid", "status", "started_at", "last_heartbeat",
                    "log_file", "config_json", "error_message",
                ])
            except Exception:
                sys.stderr.write(
                    f"CRITICAL: DB update failed for {name}. Killing PID {child.pid}.\n"
                )
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                raise

    return proc_row


def stop_process(name: str) -> PipelineProcess:
    """Stop an ad-hoc pipeline process."""
    proc_row = PipelineProcess.objects.get(name=name)
    if proc_row.status != "running" or not proc_row.pid:
        proc_row.status = "stopped"
        proc_row.save(update_fields=["status"])
        return proc_row

    try:
        os.killpg(proc_row.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _is_pid_alive(proc_row.pid):
            break
        time.sleep(0.5)
    else:
        try:
            os.killpg(proc_row.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    proc_row.status = "stopped"
    proc_row.pid = None
    proc_row.save(update_fields=["status", "pid"])
    return proc_row


def check_process(name: str) -> PipelineProcess:
    """Check the status of an ad-hoc process, fixing stale state."""
    proc_row = PipelineProcess.objects.get(name=name)
    if proc_row.status != "running":
        return proc_row

    if not proc_row.pid or not _is_pid_alive_and_matches(proc_row.pid, name):
        proc_row.status = "stopped"
        proc_row.pid = None
        proc_row.save(update_fields=["status", "pid"])
    else:
        proc_row.last_heartbeat = timezone.now()
        proc_row.save(update_fields=["last_heartbeat"])

    return proc_row


def cleanup_stale():
    """On startup, fix any stale 'running' rows."""
    for proc_row in PipelineProcess.objects.filter(status="running"):
        if not proc_row.pid or not _is_pid_alive_and_matches(proc_row.pid, proc_row.name):
            proc_row.status = "stopped"
            proc_row.pid = None
            proc_row.save(update_fields=["status", "pid"])


def read_log_tail(log_file: str | None, n_lines: int = 100) -> str:
    """Read last N lines of a log file, with path traversal protection."""
    if not log_file:
        return ""

    allowed_dir = str(LOG_DIR.resolve())
    real_path = os.path.realpath(log_file)
    if not real_path.startswith(allowed_dir + os.sep) and real_path != allowed_dir:
        return "[log path outside allowed directory]"

    try:
        with open(real_path, "r") as f:
            lines = f.readlines()
            return "".join(lines[-n_lines:])
    except (FileNotFoundError, PermissionError):
        return "[log file not available]"


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _is_pid_alive_and_matches(pid: int, expected_name: str) -> bool:
    """Check PID is alive AND its cmdline matches the expected pipeline command."""
    if not _is_pid_alive(pid):
        return False

    try:
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if cmdline_path.exists():
            cmdline = cmdline_path.read_text().replace("\x00", " ")
            return expected_name in cmdline or "lavandula" in cmdline
    except (PermissionError, OSError):
        pass

    return True

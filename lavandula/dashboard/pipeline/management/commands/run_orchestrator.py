from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from pipeline.models import CrawledOrg, Job, NonprofitSeed, Report
from pipeline.orchestrator import (
    LOG_DIR,
    PROJECT_ROOT,
    build_argv,
    check_phase_conflict,
    get_eligible_jobs,
)

POLL_INTERVAL = 10
HEARTBEAT_STALE_LOCAL = 120
HEARTBEAT_STALE_REMOTE = 300


class Command(BaseCommand):
    help = "Run the pipeline job orchestrator daemon"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shutdown = False

    def handle(self, *args, **options):
        self.hostname = socket.gethostname()
        self.stdout.write(f"Orchestrator starting on {self.hostname}")

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        LOG_DIR.mkdir(parents=True, exist_ok=True)

        self._recover_orphaned_jobs()

        self._tracked: dict[int, subprocess.Popen] = {}

        while not self._shutdown:
            self._poll_running_jobs()
            self._start_eligible_jobs()
            time.sleep(POLL_INTERVAL)

        self.stdout.write("Orchestrator shutting down")

    def _signal_handler(self, signum, frame):
        self._shutdown = True

    def _recover_orphaned_jobs(self):
        """On startup, mark dead running jobs as failed."""
        orphans = Job.objects.filter(status="running", host=self.hostname)
        for job in orphans:
            if job.pid and self._is_pid_alive(job.pid):
                continue
            job.status = "failed"
            job.error_message = "orphaned: PID not found on restart"
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error_message", "finished_at"])
            self.stdout.write(f"Marked orphaned Job #{job.pk} as failed")

    def _poll_running_jobs(self):
        """Check on tracked subprocesses and update heartbeats."""
        running = Job.objects.filter(status="running", host=self.hostname)
        for job in running:
            proc = self._tracked.get(job.pk)

            if proc is not None:
                ret = proc.poll()
                if ret is None:
                    self._update_heartbeat(job)
                else:
                    self._finish_job(job, ret)
                    self._tracked.pop(job.pk, None)
            else:
                if job.pid and self._is_pid_alive(job.pid):
                    self._update_heartbeat(job)
                else:
                    job.status = "failed"
                    job.error_message = "orphaned: PID not found"
                    job.finished_at = timezone.now()
                    self._save_with_retry(job, ["status", "error_message", "finished_at"])

    def _start_eligible_jobs(self):
        """Pick and start the next eligible job."""
        eligible = get_eligible_jobs(self.hostname)
        for job in eligible[:3]:
            if check_phase_conflict(job.phase):
                continue
            self._launch_job(job)

    def _launch_job(self, job: Job):
        """Spawn a subprocess for the job."""
        try:
            argv = build_argv(job.phase, job.config_json)
        except Exception as exc:
            job.status = "failed"
            job.error_message = f"Command build error: {exc}"
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error_message", "finished_at"])
            return

        state_label = job.state_code or "global"
        ts = timezone.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOG_DIR / f"{job.phase}_{state_label}_{ts}_{job.pk}.log"

        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}

        try:
            log_fh = open(log_path, "w")
            proc = subprocess.Popen(
                argv,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                env=env,
                preexec_fn=os.setpgrp,
            )
        except (OSError, FileNotFoundError) as exc:
            job.status = "failed"
            job.error_message = f"Spawn error: {exc}"
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error_message", "finished_at"])
            self.stderr.write(f"Failed to spawn Job #{job.pk}: {exc}")
            return

        job.status = "running"
        job.pid = proc.pid
        job.started_at = timezone.now()
        job.last_heartbeat = timezone.now()
        job.log_file = str(log_path)
        job.progress_total = self._init_progress_total(job)
        job.save(update_fields=[
            "status", "pid", "started_at", "last_heartbeat", "log_file", "progress_total",
        ])

        self._tracked[job.pk] = proc
        self.stdout.write(f"Started Job #{job.pk} ({job.phase} {state_label}) PID={proc.pid}")

    def _finish_job(self, job: Job, exit_code: int):
        """Mark a job as completed or failed based on exit code."""
        job.status = "completed" if exit_code == 0 else "failed"
        job.exit_code = exit_code
        job.finished_at = timezone.now()
        if exit_code != 0:
            job.error_message = f"Process exited with code {exit_code}"
        self._save_with_retry(job, ["status", "exit_code", "finished_at", "error_message"])
        self.stdout.write(f"Job #{job.pk} finished: {job.status} (exit {exit_code})")

    def _update_heartbeat(self, job: Job):
        job.last_heartbeat = timezone.now()
        try:
            job.save(update_fields=["last_heartbeat"])
        except Exception:
            pass

    def _save_with_retry(self, job: Job, fields: list[str]):
        """Save job state, retry once on failure, kill process on second failure."""
        try:
            job.save(update_fields=fields)
        except Exception:
            time.sleep(1)
            try:
                job.save(update_fields=fields)
            except Exception:
                sys.stderr.write(
                    f"CRITICAL: Failed to update Job #{job.pk} status. "
                    "Killing process to prevent orphan.\n"
                )
                if job.pid:
                    try:
                        os.killpg(job.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

    @staticmethod
    def _init_progress_total(job: Job):
        """Set progress_total at job start based on phase."""
        try:
            if job.phase == "resolve" and job.state_code:
                return NonprofitSeed.objects.filter(
                    state=job.state_code,
                ).exclude(resolver_status="resolved").count()
            if job.phase == "crawl":
                return NonprofitSeed.objects.filter(
                    resolver_status="resolved", website_url__isnull=False,
                ).count() - CrawledOrg.objects.count()
            if job.phase == "classify":
                return Report.objects.filter(classification__isnull=True).count()
        except Exception:
            pass
        return None

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

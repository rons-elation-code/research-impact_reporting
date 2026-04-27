import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase

from pipeline.models import Job, PipelineProcess
from pipeline.orchestrator import LOG_DIR
from pipeline.process_manager import (
    _is_pid_alive,
    _is_pid_alive_and_matches,
    cleanup_stale,
    read_log_tail,
)


class ReadLogTailTest(TestCase):
    def test_read_existing_file(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / "test_read.log"
        log_file.write_text("line1\nline2\nline3\n")
        try:
            content = read_log_tail(str(log_file), n_lines=2)
            self.assertIn("line2", content)
            self.assertIn("line3", content)
            self.assertNotIn("line1", content)
        finally:
            log_file.unlink(missing_ok=True)

    def test_read_none_returns_empty(self):
        self.assertEqual(read_log_tail(None), "")

    def test_read_missing_file(self):
        content = read_log_tail(str(LOG_DIR / "nonexistent.log"))
        self.assertIn("not available", content)

    def test_path_traversal_blocked(self):
        content = read_log_tail("/etc/passwd")
        self.assertIn("outside allowed directory", content)

    def test_relative_traversal_blocked(self):
        content = read_log_tail(str(LOG_DIR / ".." / ".." / "etc" / "passwd"))
        self.assertIn("outside allowed directory", content)


class CleanupStaleTest(TestCase):
    @patch("pipeline.process_manager._is_pid_alive_and_matches", return_value=False)
    def test_stale_processes_cleaned(self, mock_alive):
        PipelineProcess.objects.create(
            name="resolve", status="running", pid=99999
        )
        cleanup_stale()
        proc = PipelineProcess.objects.get(name="resolve")
        self.assertEqual(proc.status, "stopped")
        self.assertIsNone(proc.pid)

    @patch("pipeline.process_manager._is_pid_alive_and_matches", return_value=True)
    def test_alive_processes_kept(self, mock_alive):
        PipelineProcess.objects.create(
            name="resolve", status="running", pid=12345
        )
        cleanup_stale()
        proc = PipelineProcess.objects.get(name="resolve")
        self.assertEqual(proc.status, "running")


class PhaseConflictTest(TestCase):
    def test_running_job_conflicts_with_start(self):
        Job.objects.create(
            phase="resolve", status="running", host="localhost", state_code="NY"
        )
        from pipeline.process_manager import start_process
        with self.assertRaises(RuntimeError) as ctx:
            start_process("resolve", {})
        self.assertIn("already running", str(ctx.exception))


class PidCheckTest(TestCase):
    def test_own_pid_is_alive(self):
        self.assertTrue(_is_pid_alive(os.getpid()))

    def test_dead_pid_is_not_alive(self):
        self.assertFalse(_is_pid_alive(999999999))

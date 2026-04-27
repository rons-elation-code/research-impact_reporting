import time
from pathlib import Path

from django.core.management.base import BaseCommand

from pipeline.orchestrator import LOG_DIR

MAX_AGE_DAYS = 30


class Command(BaseCommand):
    help = "Delete dashboard log files older than 30 days"

    def handle(self, *args, **options):
        if not LOG_DIR.exists():
            self.stdout.write("Log directory does not exist, nothing to clean.")
            return

        cutoff = time.time() - (MAX_AGE_DAYS * 86400)
        deleted = 0

        for log_file in LOG_DIR.glob("*.log"):
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
                deleted += 1

        self.stdout.write(f"Deleted {deleted} log file(s) older than {MAX_AGE_DAYS} days.")

"""Reset filing_index status for operator recovery (Spec 0030).

Resets filings from error/downloaded/parsed back to 'indexed' so they
can be re-processed. Logs all resets to filing_status_audit.

Usage:
    python3 manage.py reset_990_status --ein 030440761
    python3 manage.py reset_990_status --object-id 202423190349...
    python3 manage.py reset_990_status --status error --max-rows 1000
"""
from __future__ import annotations

import os
import pwd
import logging

from django.core.management.base import BaseCommand, CommandError
from sqlalchemy import text

from lavandula.common.db import make_app_engine

log = logging.getLogger(__name__)

_ALLOWED_SOURCE_STATES = {"error", "downloaded", "parsed", "skipped"}
_LOCK_KEY = "990-family"


def _get_operator_name() -> str:
    return os.environ.get(
        "SUDO_USER", pwd.getpwuid(os.getuid()).pw_name
    )


class Command(BaseCommand):
    help = "Reset filing_index status to 'indexed' for re-processing"

    def add_arguments(self, parser):
        parser.add_argument(
            "--ein", type=str, default=None,
            help="Reset all filings for this EIN",
        )
        parser.add_argument(
            "--object-id", type=str, default=None,
            help="Reset a specific filing by object_id",
        )
        parser.add_argument(
            "--status", type=str, default=None,
            choices=sorted(_ALLOWED_SOURCE_STATES),
            help="Reset all filings with this status",
        )
        parser.add_argument(
            "--max-rows", type=int, default=None,
            help="Maximum rows to reset (required for --status bulk operations)",
        )
        parser.add_argument(
            "--yes", action="store_true",
            help="Skip confirmation prompt",
        )

    def handle(self, *args, **options):
        ein = options["ein"]
        object_id = options["object_id"]
        status_filter = options["status"]
        max_rows = options["max_rows"]
        skip_confirm = options["yes"]

        if not ein and not object_id and not status_filter:
            raise CommandError(
                "Must specify --ein, --object-id, or --status"
            )

        if status_filter and not ein and not object_id and not max_rows:
            raise CommandError(
                "--max-rows is required for bulk --status operations"
            )

        engine = make_app_engine()

        conditions = []
        params = {}

        if object_id:
            conditions.append("object_id = :oid")
            params["oid"] = object_id
        elif ein:
            conditions.append("ein = :ein")
            params["ein"] = ein

        if status_filter:
            conditions.append("status = :status")
            params["status"] = status_filter
        else:
            conditions.append("status = ANY(:allowed)")
            params["allowed"] = list(_ALLOWED_SOURCE_STATES)

        conditions.append("status != 'indexed'")
        conditions.append("status != 'batch_unresolvable'")

        where = " AND ".join(conditions)

        with engine.connect() as conn:
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM lava_corpus.filing_index WHERE {where}"),
                params,
            ).scalar()

        if count == 0:
            self.stdout.write("No filings match the criteria.")
            return

        if max_rows and count > max_rows:
            raise CommandError(
                f"Matched {count} rows exceeds --max-rows {max_rows}. "
                f"Increase --max-rows or narrow the filter."
            )

        if not skip_confirm:
            self.stdout.write(f"Will reset {count} filing(s) to 'indexed'.")
            confirm = input("Proceed? [y/N] ")
            if confirm.lower() != "y":
                self.stdout.write("Aborted.")
                return

        operator = _get_operator_name()
        cmd_args = " ".join(
            f"--{k}={v}" for k, v in options.items()
            if v and k not in ("yes", "verbosity", "settings", "pythonpath",
                               "traceback", "no_color", "force_color",
                               "skip_checks")
        )

        with engine.begin() as conn:
            rows = conn.execute(text(f"""
                SELECT object_id, status
                FROM lava_corpus.filing_index
                WHERE {where}
            """), params).fetchall()

            for row in rows:
                conn.execute(text("""
                    INSERT INTO lava_corpus.filing_status_audit
                        (filing_id, old_status, new_status, reset_by, command_args)
                    VALUES
                        (:fid, :old, 'indexed', :who, :args)
                """), {
                    "fid": row[0],
                    "old": row[1],
                    "who": operator,
                    "args": cmd_args,
                })

            result = conn.execute(text(f"""
                UPDATE lava_corpus.filing_index
                SET status = 'indexed',
                    error_message = NULL,
                    s3_xml_key = NULL
                WHERE {where}
            """), params)

        self.stdout.write(
            f"Reset {result.rowcount} filing(s) to 'indexed'. "
            f"Operator: {operator}"
        )

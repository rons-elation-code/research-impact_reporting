from __future__ import annotations

import os
import re
import signal
import time
from pathlib import Path
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Job, PipelineProcess

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = PROJECT_ROOT / "lavandula" / "logs" / "dashboard"

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
]

COMMAND_MAP: dict[str, dict[str, Any]] = {
    "seed": {
        "cmd": ["python3", "-m", "lavandula.nonprofits.tools.seed_enumerate"],
        "params": {
            "states": {"type": "text", "pattern": r"^[A-Z]{2}$", "flag": "--states"},
            "target": {"type": "int", "min": 1, "max": 999999, "flag": "--target"},
            "ntee_majors": {"type": "text", "flag": "--ntee-majors"},
            "revenue_min": {"type": "int", "min": 0, "flag": "--revenue-min"},
            "revenue_max": {"type": "int", "min": 0, "flag": "--revenue-max"},
        },
    },
    "resolve": {
        "cmd": ["python3", "-m", "lavandula.nonprofits.tools.pipeline_resolve"],
        "params": {
            "state": {"type": "choice", "choices": US_STATES, "flag": "--state"},
            "llm_url": {"type": "text", "pattern": r"^https?://", "flag": "--llm-url"},
            "llm_model": {"type": "text", "flag": "--llm-model"},
            "llm_api_key_ssm": {"type": "text", "flag": "--llm-api-key-ssm"},
            "brave_qps": {"type": "float", "min": 0.1, "max": 50.0, "flag": "--brave-qps"},
            "search_parallelism": {"type": "int", "min": 1, "max": 32, "flag": "--search-parallelism"},
            "consumer_threads": {"type": "int", "min": 1, "max": 16, "flag": "--consumer-threads"},
            "limit": {"type": "int", "min": 0, "max": 999999, "flag": "--limit"},
            "fresh_only": {"type": "bool", "flag": "--fresh-only"},
        },
    },
    "crawl": {
        "cmd": ["python3", "-m", "lavandula.reports.crawler"],
        "params": {
            "archive": {"type": "text", "pattern": r"^s3://[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9](/[a-zA-Z0-9._-]+)*$", "flag": "--archive"},
            "async": {"type": "bool", "flag": "--async"},
            "limit": {"type": "int", "min": 0, "max": 999999, "flag": "--limit"},
            "max_concurrent_orgs": {"type": "int", "min": 1, "max": 500, "flag": "--max-concurrent-orgs"},
            "max_download_workers": {"type": "int", "min": 1, "max": 100, "flag": "--max-download-workers"},
            "skip_encryption_check": {"type": "bool", "flag": "--skip-encryption-check"},
        },
    },
    "classify": {
        "cmd": ["python3", "-m", "lavandula.nonprofits.tools.pipeline_classify"],
        "params": {
            "llm_url": {"type": "text", "pattern": r"^https?://", "flag": "--llm-url"},
            "llm_model": {"type": "text", "flag": "--llm-model"},
            "llm_api_key_ssm": {"type": "text", "flag": "--llm-api-key-ssm"},
            "limit": {"type": "int", "min": 0, "max": 999999, "flag": "--limit"},
            "state": {"type": "text", "pattern": r"^[A-Z]{2}$", "flag": "--state"},
            "re_classify": {"type": "bool", "flag": "--re-classify"},
            "definition": {"type": "text", "pattern": r"^[a-z][a-z0-9_]*$", "flag": "--definition"},
            "re_classify_definition": {"type": "text", "pattern": r"^[a-z][a-z0-9_]*:v\d+$", "flag": "--re-classify-definition"},
        },
    },
    # Legacy: kept for historical job display only. No dashboard UI creates 990-enrich jobs.
    "990-enrich": {
        "cmd": ["python3", "-m", "lavandula.nonprofits.tools.enrich_990"],
        "params": {
            "state": {"type": "choice", "choices": US_STATES, "flag": "--state"},
            "years": {"type": "text", "pattern": r"^\d{4}(\s*,\s*\d{4})*$", "flag": "--years"},
            "limit": {"type": "int", "min": 1, "max": 999999, "flag": "--limit"},
        },
    },
    "990-index": {
        "cmd": ["python3", "manage.py", "load_990_index"],
        "params": {
            "ein": {"type": "text", "pattern": r"^\d{9}$", "flag": "--ein"},
            "years": {"type": "text", "pattern": r"^\d{4}(\s*,\s*\d{4})*$", "flag": "--years"},
            "current_year": {"type": "bool", "flag": "--current-year"},
        },
    },
    "990-parse": {
        "cmd": ["python3", "manage.py", "process_990_auto"],
        "params": {
            "ein": {"type": "text", "pattern": r"^\d{9}$", "flag": "--ein"},
            "reparse": {"type": "bool", "flag": "--reparse"},
            "backfill": {"type": "bool", "flag": "--backfill"},
        },
    },
}


class DuplicateJobError(Exception):
    pass


class InvalidParameterError(Exception):
    pass


def _validate_param(name: str, value: Any, spec: dict) -> str:
    """Validate and convert a single parameter. Returns the string representation."""
    ptype = spec["type"]

    if ptype == "bool":
        if not isinstance(value, bool) and value not in ("true", "false", True, False, "on"):
            raise InvalidParameterError(f"{name}: expected bool, got {value!r}")
        return ""

    if ptype == "int":
        try:
            ival = int(value)
        except (TypeError, ValueError):
            raise InvalidParameterError(f"{name}: expected int, got {value!r}")
        if "min" in spec and ival < spec["min"]:
            raise InvalidParameterError(f"{name}: {ival} < min {spec['min']}")
        if "max" in spec and ival > spec["max"]:
            raise InvalidParameterError(f"{name}: {ival} > max {spec['max']}")
        return str(ival)

    if ptype == "float":
        try:
            fval = float(value)
        except (TypeError, ValueError):
            raise InvalidParameterError(f"{name}: expected float, got {value!r}")
        if "min" in spec and fval < spec["min"]:
            raise InvalidParameterError(f"{name}: {fval} < min {spec['min']}")
        if "max" in spec and fval > spec["max"]:
            raise InvalidParameterError(f"{name}: {fval} > max {spec['max']}")
        return str(fval)

    if ptype == "choice":
        choices = spec["choices"]
        if isinstance(choices, str) and choices == "US_STATES":
            choices = US_STATES
        if value not in choices:
            raise InvalidParameterError(f"{name}: {value!r} not in allowed choices")
        return str(value)

    if ptype == "text":
        sval = str(value)
        if "pattern" in spec and not re.match(spec["pattern"], sval):
            raise InvalidParameterError(f"{name}: {sval!r} does not match pattern {spec['pattern']}")
        return sval

    raise InvalidParameterError(f"{name}: unknown type {ptype}")


def build_argv(phase: str, config_json: dict) -> list[str]:
    """Build a safe argv array from phase + config parameters."""
    if phase not in COMMAND_MAP:
        raise InvalidParameterError(f"Unknown phase: {phase}")

    entry = COMMAND_MAP[phase]
    argv = list(entry["cmd"])
    allowed = entry["params"]

    for key, value in config_json.items():
        if key not in allowed:
            raise InvalidParameterError(f"Unknown parameter {key!r} for phase {phase}")

        spec = allowed[key]
        if spec["type"] == "bool":
            if value in (True, "true", "on"):
                argv.append(spec["flag"])
        else:
            validated = _validate_param(key, value, spec)
            argv.extend([spec["flag"], validated])

    return argv


def check_phase_conflict(phase: str) -> bool:
    """Return True if a job or ad-hoc process is already running for this phase."""
    if Job.objects.filter(phase=phase, status="running").exists():
        return True
    if PipelineProcess.objects.filter(name=phase, status="running").exists():
        return True
    return False


def create_state_jobs(
    state_codes: list[str],
    phases: list[str],
    config_overrides: dict,
    host: str,
) -> list[Job]:
    """Create chained seed→resolve jobs for the given state(s).

    Allowed phase combos: ['seed'], ['resolve'], ['seed', 'resolve'].
    """
    valid_combos = [["seed"], ["resolve"], ["seed", "resolve"]]
    if sorted(phases) not in [sorted(c) for c in valid_combos]:
        raise InvalidParameterError(f"Invalid phase combination: {phases}")

    created = []
    with transaction.atomic():
        for sc in state_codes:
            for phase in phases:
                existing = (
                    Job.objects.select_for_update()
                    .filter(state_code=sc, phase=phase, status__in=["pending", "running"])
                    .first()
                )
                if existing:
                    raise DuplicateJobError(
                        f"Active {phase} job already exists for {sc}: Job #{existing.pk}"
                    )

            prev_job = None
            for phase in phases:
                allowed_keys = set(COMMAND_MAP[phase]["params"])
                config = {k: v for k, v in config_overrides.items() if k in allowed_keys}
                if phase == "seed":
                    config["states"] = sc
                elif phase == "resolve":
                    config["state"] = sc

                try:
                    job = Job.objects.create(
                        state_code=sc,
                        phase=phase,
                        status="pending",
                        host=host,
                        config_json=config,
                        depends_on=prev_job,
                    )
                except IntegrityError:
                    raise DuplicateJobError(
                        f"Duplicate {phase} job for {sc} (constraint violation)"
                    )
                created.append(job)
                prev_job = job

    return created


def create_resolve_job(config_overrides: dict, host: str) -> Job:
    """Create a resolve job. Allows queuing multiple states; blocks duplicates per state."""
    state = config_overrides.get("state")
    with transaction.atomic():
        qs = Job.objects.select_for_update().filter(
            phase="resolve", status__in=["pending", "running"],
        )
        if state:
            qs = qs.filter(state_code=state)
        existing = qs.first()
        if existing:
            label = existing.state_code or "global"
            raise DuplicateJobError(
                f"Active resolve job already exists for {label}: Job #{existing.pk}"
            )

        try:
            return Job.objects.create(
                state_code=state or None,
                phase="resolve",
                status="pending",
                host=host,
                config_json=config_overrides,
            )
        except IntegrityError:
            raise DuplicateJobError("Duplicate resolve job (constraint violation)")


_DEFAULT_ARCHIVE = "s3://lavandula-nonprofit-collaterals"


def create_crawl_job(config_overrides: dict, host: str) -> Job:
    """Create a global crawl job (state_code=NULL, no depends_on)."""
    config_overrides.setdefault("archive", _DEFAULT_ARCHIVE)
    with transaction.atomic():
        existing = (
            Job.objects.select_for_update()
            .filter(state_code__isnull=True, phase="crawl", status__in=["pending", "running"])
            .first()
        )
        if existing:
            raise DuplicateJobError(f"Active crawl job already exists: Job #{existing.pk}")

        try:
            return Job.objects.create(
                state_code=None,
                phase="crawl",
                status="pending",
                host=host,
                config_json=config_overrides,
            )
        except IntegrityError:
            raise DuplicateJobError("Duplicate crawl job (constraint violation)")


def create_classify_job(config_overrides: dict, host: str) -> Job:
    """Create a classify job, scoped to a state if provided."""
    state = config_overrides.get("state") or None
    with transaction.atomic():
        qs = Job.objects.select_for_update().filter(
            phase="classify", status__in=["pending", "running"],
        )
        if state:
            qs = qs.filter(state_code=state)
        else:
            qs = qs.filter(state_code__isnull=True)
        existing = qs.first()
        if existing:
            label = state or "global"
            raise DuplicateJobError(f"Active classify job already exists for {label}: Job #{existing.pk}")

        try:
            return Job.objects.create(
                state_code=state,
                phase="classify",
                status="pending",
                host=host,
                config_json=config_overrides,
            )
        except IntegrityError:
            raise DuplicateJobError("Duplicate classify job (constraint violation)")


_990_PHASES = {"990-enrich", "990-index", "990-parse"}


def _990_advisory_lock():
    """Acquire advisory lock for 990-family concurrency. No-op on non-PostgreSQL."""
    from django.db import connection
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('990-family'))")


def create_990_index_job(config_overrides: dict, host: str) -> Job:
    """Create a 990-index job. Blocks if any 990-family job is active."""
    state = config_overrides.get("state") or None
    ein = config_overrides.get("ein") or None
    with transaction.atomic():
        _990_advisory_lock()

        existing = Job.objects.select_for_update().filter(
            phase__in=_990_PHASES, status__in=["pending", "running"]
        ).first()
        if existing:
            raise DuplicateJobError(
                f"Active {existing.phase} job already exists: Job #{existing.pk}"
            )

        try:
            return Job.objects.create(
                state_code=state,
                phase="990-index",
                status="pending",
                host=host,
                config_json=config_overrides,
            )
        except IntegrityError:
            raise DuplicateJobError("Duplicate 990-index job (constraint violation)")


def create_990_parse_job(config_overrides: dict, host: str) -> Job:
    """Create a 990-parse job. Blocks if any 990-family job is active."""
    state = config_overrides.get("state") or None
    ein = config_overrides.get("ein") or None
    with transaction.atomic():
        _990_advisory_lock()

        existing = Job.objects.select_for_update().filter(
            phase__in=_990_PHASES, status__in=["pending", "running"]
        ).first()
        if existing:
            raise DuplicateJobError(
                f"Active {existing.phase} job already exists: Job #{existing.pk}"
            )

        try:
            return Job.objects.create(
                state_code=state,
                phase="990-parse",
                status="pending",
                host=host,
                config_json=config_overrides,
            )
        except IntegrityError:
            raise DuplicateJobError("Duplicate 990-parse job (constraint violation)")


def retry_job(job: Job) -> Job:
    """Create a new pending job from a failed job, rewiring dependents."""
    if job.status != "failed":
        raise ValueError(f"Can only retry failed jobs, got {job.status}")

    with transaction.atomic():
        new_job = Job.objects.create(
            state_code=job.state_code,
            phase=job.phase,
            status="pending",
            host=job.host,
            config_json=job.config_json,
            depends_on=job.depends_on,
        )
        Job.objects.filter(depends_on=job).update(depends_on=new_job)
        return new_job


def cancel_job(job: Job) -> None:
    """Cancel a job. If running, send SIGTERM→SIGKILL. Cascade to dependents."""
    if job.status not in ("pending", "running"):
        return

    if job.status == "running" and job.pid:
        try:
            os.killpg(job.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        else:
            time.sleep(0.5)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    os.killpg(job.pid, 0)
                except (ProcessLookupError, PermissionError):
                    break
                time.sleep(0.5)
            else:
                try:
                    os.killpg(job.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    job.status = "cancelled"
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "finished_at"])

    for dep in Job.objects.filter(depends_on=job, status__in=["pending", "running"]):
        cancel_job(dep)


def get_eligible_jobs(host: str):
    """Get jobs that are ready to run on this host."""
    from django.db.models import Q

    running_phases = set(
        Job.objects.filter(status="running").values_list("phase", flat=True)
    )
    adhoc_phases = set(
        PipelineProcess.objects.filter(status="running").values_list("name", flat=True)
    )
    blocked_phases = running_phases | adhoc_phases

    qs = (
        Job.objects.filter(status="pending", host=host)
        .filter(Q(depends_on__isnull=True) | Q(depends_on__status="completed"))
        .order_by("created_at")
    )
    if blocked_phases:
        qs = qs.exclude(phase__in=blocked_phases)

    return qs

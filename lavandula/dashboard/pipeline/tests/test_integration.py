from unittest.mock import MagicMock, patch

from django.test import TestCase

from pipeline.models import Job, PipelineProcess
from pipeline.orchestrator import (
    DuplicateJobError,
    cancel_job,
    create_crawl_job,
    create_state_jobs,
    get_eligible_jobs,
    retry_job,
)


class JobLifecycleTest(TestCase):
    """Integration: create → eligible → (mock) run → complete."""

    def test_full_lifecycle(self):
        jobs = create_state_jobs(["NY"], ["seed", "resolve"], {}, "localhost")
        seed, resolve = jobs

        eligible = get_eligible_jobs("localhost")
        self.assertIn(seed, eligible)
        self.assertNotIn(resolve, eligible)

        seed.status = "running"
        seed.pid = 12345
        seed.save()

        eligible = get_eligible_jobs("localhost")
        self.assertEqual(eligible.count(), 0)

        seed.status = "completed"
        seed.save()

        eligible = get_eligible_jobs("localhost")
        self.assertIn(resolve, eligible)


class DependencyChainTest(TestCase):
    """Seed completes → resolve becomes eligible → no auto-crawl."""

    def test_seed_resolve_chain(self):
        jobs = create_state_jobs(["MA"], ["seed", "resolve"], {}, "localhost")
        seed, resolve = jobs

        seed.status = "completed"
        seed.save()

        resolve_eligible = get_eligible_jobs("localhost")
        self.assertEqual(resolve_eligible.count(), 1)
        self.assertEqual(resolve_eligible.first().phase, "resolve")

        resolve.status = "completed"
        resolve.save()

        remaining = get_eligible_jobs("localhost")
        self.assertEqual(remaining.count(), 0)


class RetryRewiringTest(TestCase):
    """Failed job retried → dependents rewired → chain completes."""

    def test_retry_rewires_and_chain_completes(self):
        seed = Job.objects.create(
            state_code="VA", phase="seed", status="failed", host="localhost"
        )
        resolve = Job.objects.create(
            state_code="VA", phase="resolve", status="pending",
            host="localhost", depends_on=seed,
        )

        new_seed = retry_job(seed)
        resolve.refresh_from_db()
        self.assertEqual(resolve.depends_on, new_seed)

        new_seed.status = "completed"
        new_seed.save()

        eligible = get_eligible_jobs("localhost")
        self.assertIn(resolve, eligible)


class CancelCascadeTest(TestCase):
    """Cancel a job → downstream dependents also cancelled."""

    @patch("pipeline.orchestrator.os.killpg")
    def test_cancel_running_cascades(self, mock_killpg):
        mock_killpg.side_effect = ProcessLookupError

        seed = Job.objects.create(
            state_code="FL", phase="seed", status="running",
            host="localhost", pid=99999,
        )
        resolve = Job.objects.create(
            state_code="FL", phase="resolve", status="pending",
            host="localhost", depends_on=seed,
        )

        cancel_job(seed)
        seed.refresh_from_db()
        resolve.refresh_from_db()

        self.assertEqual(seed.status, "cancelled")
        self.assertEqual(resolve.status, "cancelled")

    def test_cancel_pending_cascades(self):
        seed = Job.objects.create(
            state_code="GA", phase="seed", status="pending", host="localhost"
        )
        resolve = Job.objects.create(
            state_code="GA", phase="resolve", status="pending",
            host="localhost", depends_on=seed,
        )

        cancel_job(seed)
        seed.refresh_from_db()
        resolve.refresh_from_db()
        self.assertEqual(seed.status, "cancelled")
        self.assertEqual(resolve.status, "cancelled")


class DuplicateSubmissionTest(TestCase):
    """Two submissions for same state+phase → second rejected."""

    def test_duplicate_rejected(self):
        create_state_jobs(["NY"], ["seed"], {}, "localhost")
        with self.assertRaises(DuplicateJobError):
            create_state_jobs(["NY"], ["seed"], {}, "localhost")

    def test_different_states_allowed(self):
        create_state_jobs(["NY"], ["seed"], {}, "localhost")
        jobs = create_state_jobs(["MA"], ["seed"], {}, "localhost")
        self.assertEqual(len(jobs), 1)


class CrawlIndependenceTest(TestCase):
    """Crawl jobs are independent of seed/resolve."""

    def test_crawl_immediately_eligible(self):
        Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        crawl = create_crawl_job({}, "localhost")
        eligible = get_eligible_jobs("localhost")
        phases = [j.phase for j in eligible]
        self.assertIn("crawl", phases)

    def test_crawl_no_depends_on(self):
        crawl = create_crawl_job({}, "localhost")
        self.assertIsNone(crawl.depends_on)
        self.assertIsNone(crawl.state_code)


class PhaseConflictTest(TestCase):
    """Queued jobs and ad-hoc processes are mutually exclusive per phase."""

    def test_adhoc_blocks_queued(self):
        PipelineProcess.objects.create(name="seed", status="running", pid=12345)
        Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        eligible = get_eligible_jobs("localhost")
        self.assertEqual(eligible.count(), 0)

    def test_queued_running_blocks_new_queued(self):
        Job.objects.create(
            state_code="NY", phase="seed", status="running", host="localhost"
        )
        Job.objects.create(
            state_code="MA", phase="seed", status="pending", host="localhost"
        )
        eligible = get_eligible_jobs("localhost")
        self.assertEqual(eligible.count(), 0)

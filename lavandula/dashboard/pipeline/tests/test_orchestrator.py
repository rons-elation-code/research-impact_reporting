from unittest.mock import patch

from django.test import TestCase

from pipeline.models import Job, PipelineProcess
from pipeline.orchestrator import (
    DuplicateJobError,
    InvalidParameterError,
    build_argv,
    cancel_job,
    create_990_index_job,
    create_990_parse_job,
    create_crawl_job,
    create_state_jobs,
    get_eligible_jobs,
    retry_job,
)


class CreateStateJobsTest(TestCase):
    def test_creates_seed_resolve_chain(self):
        jobs = create_state_jobs(["NY"], ["seed", "resolve"], {}, "localhost")
        self.assertEqual(len(jobs), 2)
        seed, resolve = jobs
        self.assertEqual(seed.phase, "seed")
        self.assertEqual(seed.state_code, "NY")
        self.assertIsNone(seed.depends_on)
        self.assertEqual(resolve.phase, "resolve")
        self.assertEqual(resolve.depends_on, seed)

    def test_creates_seed_only(self):
        jobs = create_state_jobs(["MA"], ["seed"], {}, "localhost")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].phase, "seed")
        self.assertIsNone(jobs[0].depends_on)

    def test_creates_resolve_only(self):
        jobs = create_state_jobs(["VA"], ["resolve"], {}, "localhost")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].phase, "resolve")
        self.assertIsNone(jobs[0].depends_on)

    def test_multi_state(self):
        jobs = create_state_jobs(["NY", "MA"], ["seed", "resolve"], {}, "localhost")
        self.assertEqual(len(jobs), 4)
        states = set(j.state_code for j in jobs)
        self.assertEqual(states, {"NY", "MA"})

    def test_config_overrides_applied(self):
        jobs = create_state_jobs(["FL"], ["seed"], {"target": 5000}, "localhost")
        self.assertEqual(jobs[0].config_json["target"], 5000)
        self.assertEqual(jobs[0].config_json["states"], "FL")

    def test_resolve_config_has_state(self):
        jobs = create_state_jobs(["GA"], ["resolve"], {}, "localhost")
        self.assertEqual(jobs[0].config_json["state"], "GA")

    def test_invalid_phase_combination(self):
        with self.assertRaises(InvalidParameterError):
            create_state_jobs(["NY"], ["crawl"], {}, "localhost")

    def test_duplicate_rejected(self):
        create_state_jobs(["NY"], ["seed"], {}, "localhost")
        with self.assertRaises(DuplicateJobError):
            create_state_jobs(["NY"], ["seed"], {}, "localhost")

    def test_completed_does_not_block(self):
        jobs = create_state_jobs(["TX"], ["seed"], {}, "localhost")
        jobs[0].status = "completed"
        jobs[0].save()
        new_jobs = create_state_jobs(["TX"], ["seed"], {}, "localhost")
        self.assertEqual(len(new_jobs), 1)

    def test_host_assigned(self):
        jobs = create_state_jobs(["OH"], ["seed"], {}, "myhost")
        self.assertEqual(jobs[0].host, "myhost")


class CreateCrawlJobTest(TestCase):
    def test_creates_crawl_job(self):
        job = create_crawl_job({}, "localhost")
        self.assertIsNone(job.state_code)
        self.assertEqual(job.phase, "crawl")
        self.assertIsNone(job.depends_on)

    def test_duplicate_crawl_rejected(self):
        create_crawl_job({}, "localhost")
        with self.assertRaises(DuplicateJobError):
            create_crawl_job({}, "localhost")


class RetryJobTest(TestCase):
    def test_retry_creates_new_job(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="failed", host="localhost"
        )
        new_job = retry_job(job)
        self.assertEqual(new_job.status, "pending")
        self.assertEqual(new_job.phase, "seed")
        self.assertEqual(new_job.state_code, "NY")
        self.assertNotEqual(new_job.pk, job.pk)

    def test_retry_rewires_dependents(self):
        seed = Job.objects.create(
            state_code="NY", phase="seed", status="failed", host="localhost"
        )
        resolve = Job.objects.create(
            state_code="NY", phase="resolve", status="pending",
            host="localhost", depends_on=seed,
        )
        new_seed = retry_job(seed)
        resolve.refresh_from_db()
        self.assertEqual(resolve.depends_on, new_seed)

    def test_retry_non_failed_raises(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        with self.assertRaises(ValueError):
            retry_job(job)


class CancelJobTest(TestCase):
    def test_cancel_pending(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        cancel_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")

    def test_cancel_cascades(self):
        seed = Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        resolve = Job.objects.create(
            state_code="NY", phase="resolve", status="pending",
            host="localhost", depends_on=seed,
        )
        cancel_job(seed)
        resolve.refresh_from_db()
        self.assertEqual(resolve.status, "cancelled")

    @patch("pipeline.orchestrator.os.killpg")
    def test_cancel_running_sends_signal(self, mock_killpg):
        mock_killpg.side_effect = ProcessLookupError
        job = Job.objects.create(
            state_code="NY", phase="seed", status="running",
            host="localhost", pid=99999,
        )
        cancel_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")

    def test_cancel_completed_noop(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="completed", host="localhost"
        )
        cancel_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")


class GetEligibleJobsTest(TestCase):
    def test_basic_eligible(self):
        Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        eligible = get_eligible_jobs("localhost")
        self.assertEqual(eligible.count(), 1)

    def test_dependency_blocks(self):
        seed = Job.objects.create(
            state_code="NY", phase="seed", status="running", host="localhost"
        )
        Job.objects.create(
            state_code="NY", phase="resolve", status="pending",
            host="localhost", depends_on=seed,
        )
        eligible = get_eligible_jobs("localhost")
        self.assertEqual(eligible.count(), 0)

    def test_completed_dependency_unblocks(self):
        seed = Job.objects.create(
            state_code="NY", phase="seed", status="completed", host="localhost"
        )
        Job.objects.create(
            state_code="NY", phase="resolve", status="pending",
            host="localhost", depends_on=seed,
        )
        eligible = get_eligible_jobs("localhost")
        self.assertEqual(eligible.count(), 1)

    def test_running_phase_blocks(self):
        Job.objects.create(
            state_code="NY", phase="seed", status="running", host="localhost"
        )
        Job.objects.create(
            state_code="MA", phase="seed", status="pending", host="localhost"
        )
        eligible = get_eligible_jobs("localhost")
        self.assertEqual(eligible.count(), 0)

    def test_different_host_not_eligible(self):
        Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="otherhost"
        )
        eligible = get_eligible_jobs("localhost")
        self.assertEqual(eligible.count(), 0)

    def test_adhoc_process_blocks_phase(self):
        PipelineProcess.objects.create(name="seed", status="running", pid=12345)
        Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        eligible = get_eligible_jobs("localhost")
        self.assertEqual(eligible.count(), 0)


class Create990IndexJobTest(TestCase):

    def test_creates_index_job(self):
        job = create_990_index_job({"state": "NY", "years": "2024"}, "localhost")
        self.assertEqual(job.phase, "990-index")
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.config_json["state"], "NY")

    def test_duplicate_index_rejected(self):
        create_990_index_job({"state": "NY", "years": "2024"}, "localhost")
        with self.assertRaises(DuplicateJobError):
            create_990_index_job({"state": "MA", "years": "2024"}, "localhost")

    def test_990_enrich_blocks_index(self):
        Job.objects.create(phase="990-enrich", status="running", host="localhost")
        with self.assertRaises(DuplicateJobError):
            create_990_index_job({"state": "NY", "years": "2024"}, "localhost")

    def test_990_parse_blocks_index(self):
        Job.objects.create(phase="990-parse", status="pending", host="localhost")
        with self.assertRaises(DuplicateJobError):
            create_990_index_job({"state": "NY", "years": "2024"}, "localhost")

    def test_completed_does_not_block(self):
        Job.objects.create(phase="990-index", status="completed", host="localhost")
        job = create_990_index_job({"state": "NY", "years": "2024"}, "localhost")
        self.assertEqual(job.phase, "990-index")

    def test_ein_mode(self):
        job = create_990_index_job({"ein": "123456789", "years": "2024"}, "localhost")
        self.assertIsNone(job.state_code)
        self.assertEqual(job.config_json["ein"], "123456789")


class Create990ParseJobTest(TestCase):

    def test_creates_parse_job(self):
        job = create_990_parse_job({"state": "NY", "years": "2024"}, "localhost")
        self.assertEqual(job.phase, "990-parse")
        self.assertEqual(job.status, "pending")

    def test_duplicate_parse_rejected(self):
        create_990_parse_job({"state": "NY", "years": "2024"}, "localhost")
        with self.assertRaises(DuplicateJobError):
            create_990_parse_job({"state": "MA", "years": "2024"}, "localhost")

    def test_990_enrich_blocks_parse(self):
        Job.objects.create(phase="990-enrich", status="running", host="localhost")
        with self.assertRaises(DuplicateJobError):
            create_990_parse_job({"state": "NY", "years": "2024"}, "localhost")

    def test_990_index_blocks_parse(self):
        Job.objects.create(phase="990-index", status="pending", host="localhost")
        with self.assertRaises(DuplicateJobError):
            create_990_parse_job({"state": "NY", "years": "2024"}, "localhost")


class BuildArgv990Test(TestCase):

    def test_990_index_state(self):
        argv = build_argv("990-index", {"state": "NY", "years": "2024"})
        self.assertIn("--index-only", argv)
        self.assertIn("--state", argv)
        self.assertIn("NY", argv)
        self.assertIn("--years", argv)
        self.assertIn("2024", argv)

    def test_990_parse_state_with_flags(self):
        argv = build_argv("990-parse", {
            "state": "NY", "years": "2024",
            "skip_download": True, "reparse": True,
        })
        self.assertIn("--parse-only", argv)
        self.assertIn("--skip-download", argv)
        self.assertIn("--reparse", argv)

    def test_990_index_ein(self):
        argv = build_argv("990-index", {"ein": "123456789", "years": "2024"})
        self.assertIn("--ein", argv)
        self.assertIn("123456789", argv)

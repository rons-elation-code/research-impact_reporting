from django.test import TestCase

from pipeline.models import Job, PipelineAuditLog, PipelineProcess


class JobModelTest(TestCase):
    def test_create_job(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        self.assertEqual(job.state_code, "NY")
        self.assertEqual(job.phase, "seed")
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.progress_current, 0)
        self.assertIsNone(job.progress_total)

    def test_create_global_job(self):
        job = Job.objects.create(
            state_code=None, phase="crawl", status="pending", host="localhost"
        )
        self.assertIsNone(job.state_code)
        self.assertEqual(str(job), f"Job {job.pk}: crawl (global) [pending]")

    def test_job_str(self):
        job = Job.objects.create(
            state_code="MA", phase="resolve", status="running", host="test"
        )
        self.assertIn("MA", str(job))
        self.assertIn("resolve", str(job))

    def test_job_dependency(self):
        j1 = Job.objects.create(phase="seed", state_code="VA", host="localhost")
        j2 = Job.objects.create(
            phase="resolve", state_code="VA", host="localhost", depends_on=j1
        )
        self.assertEqual(j2.depends_on, j1)
        self.assertIn(j2, j1.dependents.all())

    def test_default_config_json(self):
        job = Job.objects.create(phase="seed", state_code="CA", host="localhost")
        self.assertEqual(job.config_json, {})


class PipelineProcessModelTest(TestCase):
    def test_create_process(self):
        p = PipelineProcess.objects.create(name="resolve", status="stopped")
        self.assertEqual(p.name, "resolve")
        self.assertEqual(p.status, "stopped")

    def test_unique_name(self):
        PipelineProcess.objects.create(name="resolve", status="stopped")
        with self.assertRaises(Exception):
            PipelineProcess.objects.create(name="resolve", status="stopped")


class PipelineAuditLogModelTest(TestCase):
    def test_create_log(self):
        log = PipelineAuditLog.objects.create(
            action="start",
            process_name="resolve",
            parameters={"state": "NY"},
            source_ip="127.0.0.1",
        )
        self.assertEqual(log.action, "start")
        self.assertIsNotNone(log.timestamp)

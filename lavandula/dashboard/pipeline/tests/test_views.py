from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from pipeline.models import Job, PipelineAuditLog


class AuthenticationTest(TestCase):
    """Verify all views require authentication."""

    def setUp(self):
        self.client = Client()

    def test_dashboard_requires_login(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)

    def test_job_list_requires_login(self):
        resp = self.client.get(reverse("job_list"))
        self.assertEqual(resp.status_code, 302)

    def test_resolver_requires_login(self):
        resp = self.client.get(reverse("resolver"))
        self.assertEqual(resp.status_code, 302)

    def test_crawler_requires_login(self):
        resp = self.client.get(reverse("crawler"))
        self.assertEqual(resp.status_code, 302)

    def test_classifier_requires_login(self):
        resp = self.client.get(reverse("classifier"))
        self.assertEqual(resp.status_code, 302)

    def test_org_list_requires_login(self):
        resp = self.client.get(reverse("org_list"))
        self.assertEqual(resp.status_code, 302)

    def test_report_list_requires_login(self):
        resp = self.client.get(reverse("report_list"))
        self.assertEqual(resp.status_code, 302)

    def test_stats_partial_requires_login(self):
        resp = self.client.get(reverse("dashboard_stats"))
        self.assertEqual(resp.status_code, 302)


class DashboardViewTest(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("testuser", password="testpassword1234")
        self.client = Client()
        self.client.login(username="testuser", password="testpassword1234")

    def test_dashboard_renders(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pipeline Dashboard")

    def test_dashboard_stats_partial(self):
        resp = self.client.get(reverse("dashboard_stats"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Job Queue")

    def test_dashboard_with_no_data(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("jobs_running", resp.context)


class JobViewTest(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("testuser", password="testpassword1234")
        self.client = Client()
        self.client.login(username="testuser", password="testpassword1234")

    def test_job_list_renders(self):
        resp = self.client.get(reverse("job_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Job Queue")

    def test_job_detail_renders(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        resp = self.client.get(reverse("job_detail", args=[job.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"Job #{job.pk}")

    def test_job_create(self):
        resp = self.client.post(
            reverse("job_create"),
            {"state_codes": ["NY"], "phases": ["seed"]},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Job.objects.filter(phase="seed", state_code="NY").count(), 1)

    def test_job_create_multi_state(self):
        resp = self.client.post(
            reverse("job_create"),
            {"state_codes": ["NY", "MA"], "phases": ["seed", "resolve"]},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Job.objects.count(), 4)

    def test_job_cancel(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        resp = self.client.post(reverse("job_cancel", args=[job.pk]))
        self.assertEqual(resp.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")

    def test_job_retry(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="failed", host="localhost"
        )
        resp = self.client.post(reverse("job_retry", args=[job.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Job.objects.filter(status="pending").count(), 1)

    def test_job_progress_partial(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="running", host="localhost"
        )
        resp = self.client.get(reverse("job_progress", args=[job.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_job_log_partial(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="running", host="localhost"
        )
        resp = self.client.get(reverse("job_log", args=[job.pk]))
        self.assertEqual(resp.status_code, 200)


class AuditLogTest(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("testuser", password="testpassword1234")
        self.client = Client()
        self.client.login(username="testuser", password="testpassword1234")

    def test_job_create_logs_audit(self):
        self.client.post(
            reverse("job_create"),
            {"state_codes": ["NY"], "phases": ["seed"]},
        )
        self.assertEqual(PipelineAuditLog.objects.filter(action="job_create").count(), 1)
        log = PipelineAuditLog.objects.first()
        self.assertEqual(log.process_name, "state_jobs")
        self.assertIn("NY", log.parameters["states"])

    def test_job_cancel_logs_audit(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="pending", host="localhost"
        )
        self.client.post(reverse("job_cancel", args=[job.pk]))
        self.assertTrue(PipelineAuditLog.objects.filter(action="job_cancel").exists())

    def test_job_retry_logs_audit(self):
        job = Job.objects.create(
            state_code="NY", phase="seed", status="failed", host="localhost"
        )
        self.client.post(reverse("job_retry", args=[job.pk]))
        self.assertTrue(PipelineAuditLog.objects.filter(action="job_retry").exists())


class CSRFTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="testpassword1234")
        self.client = Client(enforce_csrf_checks=True)
        self.client.login(username="testuser", password="testpassword1234")

    def test_post_without_csrf_rejected(self):
        resp = self.client.post(
            reverse("job_create"),
            {"state_codes": ["NY"], "phases": ["seed"]},
        )
        self.assertEqual(resp.status_code, 403)

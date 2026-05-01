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


class EnrichIndexViewTest(TestCase):
    databases = {"default", "pipeline"}

    def setUp(self):
        self.user = User.objects.create_user("testuser", password="testpassword1234")
        self.client = Client()
        self.client.login(username="testuser", password="testpassword1234")

    def test_requires_login(self):
        client = Client()
        resp = client.get(reverse("enrich_index"))
        self.assertEqual(resp.status_code, 302)

    def test_renders(self):
        resp = self.client.get(reverse("enrich_index"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "990 Index Controls")

    def test_context_keys(self):
        resp = self.client.get(reverse("enrich_index"))
        self.assertIn("form", resp.context)
        self.assertIn("status_counts", resp.context)
        self.assertIn("total_filings", resp.context)
        self.assertIn("scoped", resp.context)

    def test_unscoped_by_default(self):
        resp = self.client.get(reverse("enrich_index"))
        self.assertFalse(resp.context["scoped"])

    def test_job_create_post_only(self):
        resp = self.client.get(reverse("enrich_index_job_create"))
        self.assertEqual(resp.status_code, 405)

    def test_job_create_valid(self):
        resp = self.client.post(
            reverse("enrich_index_job_create"),
            {"state": "NY", "years": "2024"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Job.objects.filter(phase="990-index").count(), 1)

    def test_job_create_duplicate(self):
        Job.objects.create(phase="990-index", status="pending", host="localhost")
        resp = self.client.post(
            reverse("enrich_index_job_create"),
            {"state": "NY", "years": "2024"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Job.objects.filter(phase="990-index").count(), 1)

    def test_job_create_logs_audit(self):
        self.client.post(
            reverse("enrich_index_job_create"),
            {"state": "NY", "years": "2024"},
        )
        self.assertTrue(
            PipelineAuditLog.objects.filter(
                action="job_create", process_name="990-index"
            ).exists()
        )


class EnrichParseViewTest(TestCase):
    databases = {"default", "pipeline"}

    def setUp(self):
        self.user = User.objects.create_user("testuser", password="testpassword1234")
        self.client = Client()
        self.client.login(username="testuser", password="testpassword1234")

    def test_requires_login(self):
        client = Client()
        resp = client.get(reverse("enrich_parse"))
        self.assertEqual(resp.status_code, 302)

    def test_renders(self):
        resp = self.client.get(reverse("enrich_parse"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "990 Parse Controls")

    def test_context_keys(self):
        resp = self.client.get(reverse("enrich_parse"))
        self.assertIn("form", resp.context)
        self.assertIn("people_count", resp.context)
        self.assertIn("cache_count", resp.context)

    def test_job_create_post_only(self):
        resp = self.client.get(reverse("enrich_parse_job_create"))
        self.assertEqual(resp.status_code, 405)

    def test_job_create_valid(self):
        resp = self.client.post(
            reverse("enrich_parse_job_create"),
            {"state": "NY", "years": "2024"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Job.objects.filter(phase="990-parse").count(), 1)

    def test_job_create_duplicate(self):
        Job.objects.create(phase="990-parse", status="pending", host="localhost")
        resp = self.client.post(
            reverse("enrich_parse_job_create"),
            {"state": "NY", "years": "2024"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Job.objects.filter(phase="990-parse").count(), 1)

    def test_990_enrich_blocks_990_parse(self):
        Job.objects.create(phase="990-enrich", status="running", host="localhost")
        resp = self.client.post(
            reverse("enrich_parse_job_create"),
            {"state": "NY", "years": "2024"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.filter(phase="990-parse").exists())


class OrgDetail990Test(TestCase):
    databases = {"default", "pipeline"}

    def setUp(self):
        from pipeline.models import NonprofitSeed
        self.user = User.objects.create_user("testuser", password="testpassword1234")
        self.client = Client()
        self.client.login(username="testuser", password="testpassword1234")
        NonprofitSeed.objects.create(ein="123456789", name="Test Org")

    def test_org_detail_zero_filings(self):
        resp = self.client.get(reverse("org_detail", args=["123456789"]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No 990 filings found")

    def _create_filing(self, **kwargs):
        from pipeline.models import FilingIndex
        return FilingIndex.objects.using("pipeline").create(**kwargs)

    def _create_person(self, **kwargs):
        from pipeline.models import Person
        return Person.objects.using("pipeline").create(**kwargs)

    def test_org_detail_with_filings(self):
        self._create_filing(
            object_id="OBJ001", ein="123456789", tax_period="202312",
            return_type="990", filing_year=2023, status="parsed",
        )
        resp = self.client.get(reverse("org_detail", args=["123456789"]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("selected_filing", resp.context)
        self.assertEqual(resp.context["selected_filing"].object_id, "OBJ001")

    def test_org_detail_filing_picker_default(self):
        self._create_filing(
            object_id="OBJ001", ein="123456789", tax_period="202212",
            return_type="990", filing_year=2022, status="parsed",
        )
        self._create_filing(
            object_id="OBJ002", ein="123456789", tax_period="202312",
            return_type="990", filing_year=2023, status="parsed",
        )
        resp = self.client.get(reverse("org_detail", args=["123456789"]))
        self.assertEqual(resp.context["selected_filing"].object_id, "OBJ002")

    def test_org_detail_filing_picker_param(self):
        self._create_filing(
            object_id="OBJ001", ein="123456789", tax_period="202212",
            return_type="990", filing_year=2022, status="parsed",
        )
        self._create_filing(
            object_id="OBJ002", ein="123456789", tax_period="202312",
            return_type="990", filing_year=2023, status="parsed",
        )
        resp = self.client.get(reverse("org_detail", args=["123456789"]) + "?filing=OBJ001")
        self.assertEqual(resp.context["selected_filing"].object_id, "OBJ001")

    def test_org_detail_invalid_filing_fallback(self):
        self._create_filing(
            object_id="OBJ001", ein="123456789", tax_period="202312",
            return_type="990", filing_year=2023, status="parsed",
        )
        resp = self.client.get(reverse("org_detail", args=["123456789"]) + "?filing=INVALID")
        self.assertEqual(resp.context["selected_filing"].object_id, "OBJ001")

    def test_org_detail_wrong_ein_filing_fallback(self):
        self._create_filing(
            object_id="OBJ001", ein="123456789", tax_period="202312",
            return_type="990", filing_year=2023, status="parsed",
        )
        self._create_filing(
            object_id="OBJ999", ein="999999999", tax_period="202312",
            return_type="990", filing_year=2023, status="parsed",
        )
        resp = self.client.get(reverse("org_detail", args=["123456789"]) + "?filing=OBJ999")
        self.assertEqual(resp.context["selected_filing"].object_id, "OBJ001")

    def test_org_detail_zero_people(self):
        self._create_filing(
            object_id="OBJ001", ein="123456789", tax_period="202312",
            return_type="990", filing_year=2023, status="parsed",
        )
        resp = self.client.get(reverse("org_detail", args=["123456789"]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No leadership data extracted")

    def test_org_detail_comparison_multiple_filings(self):
        self._create_filing(
            object_id="OBJ001", ein="123456789", tax_period="202212",
            return_type="990", filing_year=2022, status="parsed",
        )
        self._create_filing(
            object_id="OBJ002", ein="123456789", tax_period="202312",
            return_type="990", filing_year=2023, status="parsed",
        )
        resp = self.client.get(reverse("org_detail", args=["123456789"]))
        self.assertIn("comparison", resp.context)
        self.assertIn("filing_headers", resp.context)

    def test_org_detail_no_comparison_single_filing(self):
        self._create_filing(
            object_id="OBJ001", ein="123456789", tax_period="202312",
            return_type="990", filing_year=2023, status="parsed",
        )
        resp = self.client.get(reverse("org_detail", args=["123456789"]))
        self.assertNotIn("comparison", resp.context)


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

    def test_990_index_csrf(self):
        resp = self.client.post(
            reverse("enrich_index_job_create"),
            {"state": "NY", "years": "2024"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_990_parse_csrf(self):
        resp = self.client.post(
            reverse("enrich_parse_job_create"),
            {"state": "NY", "years": "2024"},
        )
        self.assertEqual(resp.status_code, 403)

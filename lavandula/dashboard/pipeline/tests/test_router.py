from django.test import TestCase

from pipeline.models import Job, NonprofitSeed, PipelineProcess, Report
from pipeline.routers import PipelineRouter


class PipelineRouterTest(TestCase):
    def setUp(self):
        self.router = PipelineRouter()

    def test_unmanaged_model_reads_from_pipeline(self):
        self.assertEqual(self.router.db_for_read(NonprofitSeed), "pipeline")
        self.assertEqual(self.router.db_for_read(Report), "pipeline")

    def test_managed_model_reads_from_default(self):
        self.assertEqual(self.router.db_for_read(Job), "default")
        self.assertEqual(self.router.db_for_read(PipelineProcess), "default")

    def test_unmanaged_model_write_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.router.db_for_write(NonprofitSeed)
        self.assertIn("Write blocked", str(ctx.exception))
        self.assertIn("unmanaged", str(ctx.exception))

    def test_managed_model_writes_to_default(self):
        self.assertEqual(self.router.db_for_write(Job), "default")

    def test_migrations_blocked_on_pipeline(self):
        self.assertFalse(self.router.allow_migrate("pipeline", "pipeline"))

    def test_migrations_allowed_on_default(self):
        self.assertTrue(self.router.allow_migrate("default", "pipeline"))

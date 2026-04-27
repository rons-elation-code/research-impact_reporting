from django.test import TestCase

from pipeline.orchestrator import InvalidParameterError, build_argv


class BuildArgvTest(TestCase):
    def test_seed_basic(self):
        argv = build_argv("seed", {"states": "NY"})
        self.assertEqual(
            argv,
            ["python3", "-m", "lavandula.nonprofits.tools.seed_enumerate", "--states", "NY"],
        )

    def test_seed_with_target(self):
        argv = build_argv("seed", {"states": "CA", "target": 1000})
        self.assertIn("--target", argv)
        self.assertIn("1000", argv)

    def test_resolve_basic(self):
        argv = build_argv("resolve", {"state": "MA"})
        self.assertEqual(
            argv,
            ["python3", "-m", "lavandula.nonprofits.tools.pipeline_resolve", "--state", "MA"],
        )

    def test_resolve_with_options(self):
        argv = build_argv("resolve", {
            "state": "VA",
            "brave_qps": 5.0,
            "consumer_threads": 4,
            "fresh_only": True,
        })
        self.assertIn("--brave-qps", argv)
        self.assertIn("5.0", argv)
        self.assertIn("--consumer-threads", argv)
        self.assertIn("4", argv)
        self.assertIn("--fresh-only", argv)

    def test_crawl_basic(self):
        argv = build_argv("crawl", {})
        self.assertEqual(
            argv, ["python3", "-m", "lavandula.reports.crawler"]
        )

    def test_crawl_with_options(self):
        argv = build_argv("crawl", {
            "archive": "s3://mybucket/path",
            "limit": 100,
            "max_concurrent_orgs": 50,
        })
        self.assertIn("--archive", argv)
        self.assertIn("s3://mybucket/path", argv)
        self.assertIn("--limit", argv)
        self.assertIn("100", argv)

    def test_classify_basic(self):
        argv = build_argv("classify", {"llm_model": "gpt-4o-mini"})
        self.assertIn("--llm-model", argv)
        self.assertIn("gpt-4o-mini", argv)

    def test_bool_false_excluded(self):
        argv = build_argv("resolve", {"state": "NY", "fresh_only": False})
        self.assertNotIn("--fresh-only", argv)

    def test_unknown_phase_rejected(self):
        with self.assertRaises(InvalidParameterError):
            build_argv("nonexistent", {})

    def test_unknown_param_rejected(self):
        with self.assertRaises(InvalidParameterError):
            build_argv("seed", {"bad_param": "value"})

    def test_int_out_of_range_rejected(self):
        with self.assertRaises(InvalidParameterError):
            build_argv("resolve", {"state": "NY", "consumer_threads": 999})

    def test_int_below_min_rejected(self):
        with self.assertRaises(InvalidParameterError):
            build_argv("resolve", {"state": "NY", "consumer_threads": 0})

    def test_float_out_of_range_rejected(self):
        with self.assertRaises(InvalidParameterError):
            build_argv("resolve", {"state": "NY", "brave_qps": 100.0})

    def test_text_pattern_rejected(self):
        with self.assertRaises(InvalidParameterError):
            build_argv("seed", {"states": "invalid"})

    def test_choice_invalid_rejected(self):
        with self.assertRaises(InvalidParameterError):
            build_argv("resolve", {"state": "XX"})

    def test_int_type_invalid(self):
        with self.assertRaises(InvalidParameterError):
            build_argv("seed", {"target": "not_a_number"})

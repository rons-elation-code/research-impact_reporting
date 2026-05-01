from django.test import TestCase

from pipeline.templatetags.pipeline_tags import (
    currency,
    dictget,
    filing_badge,
    person_badge,
)


class CurrencyFilterTest(TestCase):

    def test_positive_integer(self):
        self.assertEqual(currency(123456), "$123,456")

    def test_zero(self):
        self.assertEqual(currency(0), "$0")

    def test_none(self):
        self.assertEqual(currency(None), "—")

    def test_large_number(self):
        self.assertEqual(currency(1000000), "$1,000,000")

    def test_negative(self):
        self.assertEqual(currency(-500), "$-500")


class PersonBadgeTest(TestCase):

    def test_officer(self):
        self.assertIn("blue", person_badge("officer"))

    def test_director(self):
        self.assertIn("gray", person_badge("director"))

    def test_key_employee(self):
        self.assertIn("green", person_badge("key_employee"))

    def test_highest_compensated(self):
        self.assertIn("amber", person_badge("highest_compensated"))

    def test_unknown_type(self):
        self.assertIn("gray", person_badge("unknown"))


class FilingBadgeTest(TestCase):

    def test_parsed(self):
        self.assertIn("green", filing_badge("parsed"))

    def test_error(self):
        self.assertIn("red", filing_badge("error"))

    def test_downloaded(self):
        self.assertIn("blue", filing_badge("downloaded"))

    def test_indexed(self):
        self.assertIn("gray", filing_badge("indexed"))


class DictgetFilterTest(TestCase):

    def test_existing_key(self):
        self.assertEqual(dictget({"a": 1}, "a"), 1)

    def test_missing_key(self):
        self.assertIsNone(dictget({"a": 1}, "b"))

    def test_non_dict(self):
        self.assertIsNone(dictget("not a dict", "key"))

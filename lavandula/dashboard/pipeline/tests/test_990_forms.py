import datetime

from django.test import TestCase

from pipeline.forms import EnrichIndexForm, EnrichParseForm


class EnrichIndexFormTest(TestCase):

    def test_valid_state_only(self):
        form = EnrichIndexForm({"state": "NY", "years": "2024"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_ein_only(self):
        form = EnrichIndexForm({"ein": "123456789", "years": "2024"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_both_blank_rejected(self):
        form = EnrichIndexForm({"state": "", "ein": "", "years": "2024"})
        self.assertFalse(form.is_valid())
        self.assertIn("State or EIN is required", str(form.errors))

    def test_invalid_ein_format(self):
        form = EnrichIndexForm({"ein": "12345", "years": "2024"})
        self.assertFalse(form.is_valid())
        self.assertIn("9 digits", str(form.errors))

    def test_invalid_years_format(self):
        form = EnrichIndexForm({"state": "NY", "years": "abcd"})
        self.assertFalse(form.is_valid())
        self.assertIn("comma-separated", str(form.errors))

    def test_more_than_5_years_rejected(self):
        form = EnrichIndexForm({"state": "NY", "years": "2019,2020,2021,2022,2023,2024"})
        self.assertFalse(form.is_valid())
        self.assertIn("Maximum 5 years", str(form.errors))

    def test_year_out_of_range_low(self):
        form = EnrichIndexForm({"state": "NY", "years": "2018"})
        self.assertFalse(form.is_valid())
        self.assertIn("outside valid range", str(form.errors))

    def test_year_out_of_range_high(self):
        future = datetime.date.today().year + 1
        form = EnrichIndexForm({"state": "NY", "years": str(future)})
        self.assertFalse(form.is_valid())
        self.assertIn("outside valid range", str(form.errors))

    def test_multiple_valid_years(self):
        form = EnrichIndexForm({"state": "NY", "years": "2023,2024"})
        self.assertTrue(form.is_valid(), form.errors)


class EnrichParseFormTest(TestCase):

    def test_valid_with_all_fields(self):
        form = EnrichParseForm({
            "state": "CA",
            "years": "2024",
            "limit": 100,
            "skip_download": True,
            "reparse": True,
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_both_blank_rejected(self):
        form = EnrichParseForm({"state": "", "ein": "", "years": "2024"})
        self.assertFalse(form.is_valid())

    def test_limit_bounds(self):
        form = EnrichParseForm({"state": "NY", "years": "2024", "limit": 0})
        self.assertFalse(form.is_valid())

    def test_limit_max(self):
        form = EnrichParseForm({"state": "NY", "years": "2024", "limit": 1000000})
        self.assertFalse(form.is_valid())

    def test_checkbox_defaults_false(self):
        form = EnrichParseForm({"state": "NY", "years": "2024"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data["skip_download"])
        self.assertFalse(form.cleaned_data["reparse"])

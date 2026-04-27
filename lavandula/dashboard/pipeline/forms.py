from django import forms

from .orchestrator import US_STATES

STATE_CHOICES = [(s, s) for s in US_STATES]
PHASE_CHOICES = [("seed", "Seed"), ("resolve", "Resolve")]


class RunStateForm(forms.Form):
    state_codes = forms.MultipleChoiceField(
        choices=STATE_CHOICES,
        widget=forms.SelectMultiple(attrs={
            "class": "w-full border border-gray-300 rounded px-3 py-2",
            "size": "6",
        }),
    )
    phases = forms.MultipleChoiceField(
        choices=PHASE_CHOICES,
        initial=["seed", "resolve"],
        widget=forms.CheckboxSelectMultiple,
    )
    llm_model = forms.CharField(required=False, widget=forms.TextInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2", "placeholder": "e.g. gpt-4o-mini"}
    ))
    brave_qps = forms.FloatField(required=False, min_value=0.1, max_value=50.0, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2", "step": "0.1"}
    ))
    consumer_threads = forms.IntegerField(required=False, min_value=1, max_value=16, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    search_parallelism = forms.IntegerField(required=False, min_value=1, max_value=32, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    limit = forms.IntegerField(required=False, min_value=0, max_value=999999, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))


class RunCrawlForm(forms.Form):
    archive = forms.CharField(required=False, widget=forms.TextInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2", "placeholder": "s3://bucket/path"}
    ))
    limit = forms.IntegerField(required=False, min_value=0, max_value=999999, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    max_concurrent_orgs = forms.IntegerField(required=False, min_value=1, max_value=500, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    max_download_workers = forms.IntegerField(required=False, min_value=1, max_value=100, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    skip_encryption_check = forms.BooleanField(required=False)
    async_mode = forms.BooleanField(required=False, label="Async")


class ResolverForm(forms.Form):
    state = forms.ChoiceField(
        choices=[("", "All states")] + STATE_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}),
    )
    llm_model = forms.CharField(required=False, widget=forms.TextInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    brave_qps = forms.FloatField(required=False, min_value=0.1, max_value=50.0, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2", "step": "0.1"}
    ))
    consumer_threads = forms.IntegerField(required=False, min_value=1, max_value=16, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    limit = forms.IntegerField(required=False, min_value=0, max_value=999999, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    fresh_only = forms.BooleanField(required=False)


class CrawlerForm(forms.Form):
    archive = forms.CharField(required=False, widget=forms.TextInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2", "placeholder": "s3://bucket/path"}
    ))
    limit = forms.IntegerField(required=False, min_value=0, max_value=999999, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    max_concurrent_orgs = forms.IntegerField(required=False, min_value=1, max_value=500, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    max_download_workers = forms.IntegerField(required=False, min_value=1, max_value=100, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))


class ClassifierForm(forms.Form):
    llm_model = forms.CharField(required=False, widget=forms.TextInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))
    limit = forms.IntegerField(required=False, min_value=0, max_value=999999, widget=forms.NumberInput(
        attrs={"class": "w-full border border-gray-300 rounded px-3 py-2"}
    ))

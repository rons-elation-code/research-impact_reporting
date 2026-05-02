import datetime
import re
from pathlib import Path

from django import forms

from .orchestrator import US_STATES

STATE_CHOICES = [(s, s) for s in US_STATES]
PHASE_CHOICES = [("seed", "Seed"), ("resolve", "Resolve")]

_SELECT = "w-full border border-gray-300 rounded px-3 py-2"

LLM_PRESETS = {
    "deepseek-v4-flash": {
        "llm_url": "https://api.deepseek.com/v1",
        "llm_model": "deepseek-v4-flash",
        "llm_api_key_ssm": "lavandula/deepseek/api_key",
    },
    "local-ollama": {
        "llm_url": "http://localhost:11434/v1",
        "llm_model": "gemma4:e4b",
    },
}

LLM_PRESET_CHOICES = [
    ("deepseek-v4-flash", "DeepSeek v4-flash (API)"),
    ("local-ollama", "Local Ollama (gemma4)"),
]

SEARCH_ENGINE_CHOICES = [
    ("brave", "Brave (default)"),
    ("google", "Google"),
    ("brave_google", "Brave + Google"),
    ("auto", "Auto (Serpex routing)"),
]


ALL_NTEE_MAJORS = "A,B,C,D,E,F,G,H,I,J,K,L,M,N,O,P,Q,R,S,T,U,V,W,X,Y,Z"


class RunStateForm(forms.Form):
    state_codes = forms.MultipleChoiceField(
        choices=STATE_CHOICES,
        widget=forms.SelectMultiple(attrs={"class": _SELECT, "size": "6"}),
    )
    phases = forms.MultipleChoiceField(
        choices=PHASE_CHOICES,
        initial=["seed", "resolve"],
        widget=forms.CheckboxSelectMultiple,
    )
    ntee_majors = forms.CharField(
        initial=ALL_NTEE_MAJORS,
        widget=forms.TextInput(attrs={"class": _SELECT}),
        label="NTEE Majors",
        help_text="Comma-separated letter codes",
    )
    revenue_min = forms.IntegerField(
        initial=500000,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": _SELECT}),
        label="Revenue Min",
    )
    revenue_max = forms.IntegerField(
        initial=999999999999,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": _SELECT}),
        label="Revenue Max",
    )
    target = forms.IntegerField(
        initial=999999,
        min_value=1, max_value=999999,
        widget=forms.NumberInput(attrs={"class": _SELECT}),
        label="Target",
    )
    llm_preset = forms.ChoiceField(
        choices=LLM_PRESET_CHOICES,
        initial="deepseek-v4-flash",
        widget=forms.Select(attrs={"class": _SELECT}),
        label="LLM (resolve phase)",
    )
    brave_qps = forms.FloatField(required=False, min_value=0.1, max_value=50.0, widget=forms.NumberInput(
        attrs={"class": _SELECT, "step": "0.1"}
    ))
    consumer_threads = forms.IntegerField(required=False, min_value=1, max_value=16, widget=forms.NumberInput(
        attrs={"class": _SELECT}
    ))
    search_parallelism = forms.IntegerField(required=False, min_value=1, max_value=32, widget=forms.NumberInput(
        attrs={"class": _SELECT}
    ))
    limit = forms.IntegerField(required=False, min_value=0, max_value=999999, widget=forms.NumberInput(
        attrs={"class": _SELECT}
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
        choices=[("", "— Select state —")] + STATE_CHOICES,
        widget=forms.Select(attrs={"class": _SELECT}),
    )
    llm_preset = forms.ChoiceField(
        choices=LLM_PRESET_CHOICES,
        initial="deepseek-v4-flash",
        widget=forms.Select(attrs={"class": _SELECT}),
        label="LLM",
    )
    search_engines = forms.ChoiceField(
        choices=SEARCH_ENGINE_CHOICES,
        initial="brave",
        widget=forms.Select(attrs={"class": _SELECT}),
        label="Search Engine",
    )
    brave_qps = forms.FloatField(initial=10.0, required=False, min_value=0.1, max_value=50.0, widget=forms.NumberInput(
        attrs={"class": _SELECT, "step": "0.1"}
    ), label="Search QPS")
    search_parallelism = forms.IntegerField(initial=12, required=False, min_value=1, max_value=32, widget=forms.NumberInput(
        attrs={"class": _SELECT}
    ))
    consumer_threads = forms.IntegerField(initial=4, required=False, min_value=1, max_value=16, widget=forms.NumberInput(
        attrs={"class": _SELECT}
    ))
    limit = forms.IntegerField(required=False, min_value=0, max_value=999999, widget=forms.NumberInput(
        attrs={"class": _SELECT}
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


def _get_definition_choices():
    """Scan definitions/ directory for available .md files."""
    defn_dir = Path(__file__).resolve().parents[2] / "nonprofits" / "definitions"
    choices = []
    if defn_dir.is_dir():
        for f in sorted(defn_dir.glob("*.md")):
            if re.match(r"^[a-z][a-z0-9_]*$", f.stem):
                choices.append((f.stem, f.stem))
    if not choices:
        choices = [("corpus_reports", "corpus_reports")]
    return choices


class ClassifierForm(forms.Form):
    state = forms.ChoiceField(
        choices=[("", "All states")] + STATE_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": _SELECT}),
    )
    llm_preset = forms.ChoiceField(
        choices=LLM_PRESET_CHOICES,
        initial="deepseek-v4-flash",
        widget=forms.Select(attrs={"class": _SELECT}),
        label="LLM",
    )
    definition = forms.ChoiceField(
        choices=_get_definition_choices,
        initial="corpus_reports",
        widget=forms.Select(attrs={"class": _SELECT}),
        label="Definition",
    )
    limit = forms.IntegerField(required=False, min_value=0, max_value=999999, widget=forms.NumberInput(
        attrs={"class": _SELECT}
    ))
    re_classify = forms.BooleanField(required=False, label="Re-classify")


def _clean_990_common(cleaned_data):
    """Shared validation for 990 index/parse forms."""
    ein = cleaned_data.get("ein")
    if ein and not re.match(r"^\d{9}$", ein):
        raise forms.ValidationError("EIN must be exactly 9 digits.")
    years_str = cleaned_data.get("years", "").strip()
    if years_str:
        if not re.match(r"^\d{4}(\s*,\s*\d{4})*$", years_str):
            raise forms.ValidationError("Years must be comma-separated 4-digit years.")
        year_list = [int(y.strip()) for y in years_str.split(",")]
        current_year = datetime.date.today().year
        for y in year_list:
            if y < 2017 or y > current_year:
                raise forms.ValidationError(
                    f"Year {y} outside valid range [2017, {current_year}]."
                )
        if len(year_list) > 10:
            raise forms.ValidationError("Maximum 10 years per request.")
    return cleaned_data


class EnrichIndexForm(forms.Form):
    ein = forms.CharField(
        max_length=9, required=False,
        widget=forms.TextInput(attrs={"class": _SELECT, "placeholder": "EIN (optional)"}),
    )
    years = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": _SELECT, "placeholder": "2024,2025 (optional)"}),
    )

    def clean(self):
        return _clean_990_common(super().clean())


class EnrichParseForm(forms.Form):
    ein = forms.CharField(
        max_length=9, required=False,
        widget=forms.TextInput(attrs={"class": _SELECT, "placeholder": "EIN (optional)"}),
    )
    reparse = forms.BooleanField(required=False, label="Reparse errors")
    backfill = forms.BooleanField(required=False, label="Backfill (all unprocessed)")

    def clean(self):
        return _clean_990_common(super().clean())


class PhoneEnrichForm(forms.Form):
    state = forms.ChoiceField(
        choices=[("", "All resolved")] + STATE_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": _SELECT}),
    )
    limit = forms.IntegerField(required=False, min_value=0, max_value=999999,
        widget=forms.NumberInput(attrs={"class": _SELECT}))
    search_engines = forms.ChoiceField(
        choices=SEARCH_ENGINE_CHOICES, initial="brave",
        widget=forms.Select(attrs={"class": _SELECT}),
        label="Search Engine",
    )

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
    brave_qps = forms.FloatField(required=False, min_value=0.1, max_value=50.0, widget=forms.NumberInput(
        attrs={"class": _SELECT, "step": "0.1"}
    ))
    search_parallelism = forms.IntegerField(required=False, min_value=1, max_value=32, widget=forms.NumberInput(
        attrs={"class": _SELECT}
    ))
    consumer_threads = forms.IntegerField(required=False, min_value=1, max_value=16, widget=forms.NumberInput(
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


class ClassifierForm(forms.Form):
    llm_preset = forms.ChoiceField(
        choices=LLM_PRESET_CHOICES,
        initial="deepseek-v4-flash",
        widget=forms.Select(attrs={"class": _SELECT}),
        label="LLM",
    )
    limit = forms.IntegerField(required=False, min_value=0, max_value=999999, widget=forms.NumberInput(
        attrs={"class": _SELECT}
    ))

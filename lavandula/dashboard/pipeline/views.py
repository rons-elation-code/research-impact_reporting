import socket

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView


class HtmxLoginRequiredMixin(LoginRequiredMixin):
    """LoginRequiredMixin that returns HX-Redirect for HTMX requests."""

    def handle_no_permission(self):
        if self.request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = self.get_login_url()
            return response
        return super().handle_no_permission()

from .models import (
    CrawledOrg,
    FilingIndex,
    Job,
    NonprofitSeed,
    Person,
    PipelineAuditLog,
    PipelineProcess,
    Report,
)
from .orchestrator import (
    DuplicateJobError,
    InvalidParameterError,
    cancel_job,
    create_990_index_job,
    create_990_parse_job,
    create_classify_job,
    create_crawl_job,
    create_resolve_job,
    create_state_jobs,
    retry_job,
)
from .process_manager import check_process, read_log_tail, start_process, stop_process


def _log_audit(request, action, process_name, parameters=None):
    ip = request.META.get("REMOTE_ADDR", "127.0.0.1")
    PipelineAuditLog.objects.create(
        action=action,
        process_name=process_name,
        parameters=parameters or {},
        source_ip=ip,
    )


def _get_hostname():
    return socket.gethostname()


def _expand_llm_preset(config: dict) -> dict:
    """Replace llm_preset key with llm_url, llm_model, llm_api_key_ssm."""
    from .forms import LLM_PRESETS
    preset_key = config.pop("llm_preset", None)
    if preset_key and preset_key in LLM_PRESETS:
        config.update(LLM_PRESETS[preset_key])
    return config


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(_dashboard_stats())
        return ctx


class DashboardStatsPartial(HtmxLoginRequiredMixin, TemplateView):
    template_name = "pipeline/partials/dashboard_stats.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(_dashboard_stats())
        return ctx


def _dashboard_stats():
    stats = {}
    stats["jobs_running"] = Job.objects.filter(status="running").count()
    stats["jobs_pending"] = Job.objects.filter(status="pending").count()
    stats["jobs_completed"] = Job.objects.filter(status="completed").count()
    stats["jobs_failed"] = Job.objects.filter(status="failed").count()

    stats["seed_total"] = NonprofitSeed.objects.count()
    stats["seed_by_status"] = list(
        NonprofitSeed.objects.values_list("resolver_status")
        .annotate(c=Count("ein"))
        .order_by("-c")[:10]
    )

    stats["resolver_resolved"] = NonprofitSeed.objects.filter(
        resolver_status="resolved"
    ).count()
    stats["resolver_unresolved"] = NonprofitSeed.objects.exclude(
        resolver_status="resolved"
    ).count()
    stats["resolver_by_method"] = list(
        NonprofitSeed.objects.filter(resolver_method__isnull=False)
        .values_list("resolver_method")
        .annotate(c=Count("ein"))
        .order_by("-c")[:10]
    )

    stats["crawler_orgs"] = CrawledOrg.objects.count()
    stats["crawler_reports"] = Report.objects.count()

    stats["classifier_done"] = Report.objects.filter(
        classification__isnull=False
    ).count()
    stats["classifier_pending"] = Report.objects.filter(
        classification__isnull=True
    ).count()
    stats["classifier_by_type"] = list(
        Report.objects.filter(classification__isnull=False)
        .values_list("classification")
        .annotate(c=Count("content_sha256"))
        .order_by("-c")[:10]
    )

    stats["reports_total"] = Report.objects.count()
    stats["reports_by_year"] = list(
        Report.objects.values_list("report_year")
        .annotate(c=Count("content_sha256"))
        .order_by("-report_year")[:10]
    )

    return stats


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class SeederView(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/seeder.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from .forms import RunStateForm
        ctx["form"] = RunStateForm()
        ctx["running_job"] = Job.objects.filter(phase="seed", status="running").first()
        ctx["recent_jobs"] = Job.objects.filter(phase="seed").order_by("-created_at")[:20]
        ctx["seed_stats"] = (
            NonprofitSeed.objects.values("state")
            .annotate(count=Count("ein"))
            .order_by("state")
        )
        return ctx


class JobListView(HtmxLoginRequiredMixin, TemplateView):
    template_name = "pipeline/jobs.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_jobs"] = Job.objects.filter(status="running").order_by("-started_at")
        ctx["pending_jobs"] = Job.objects.filter(status="pending").order_by("created_at")
        ctx["history_jobs"] = Job.objects.filter(
            status__in=["completed", "failed", "cancelled"]
        ).order_by("-finished_at")[:50]
        return ctx


class JobDetailView(LoginRequiredMixin, DetailView):
    model = Job
    template_name = "pipeline/job_detail.html"
    context_object_name = "job"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["log_content"] = read_log_tail(self.object.log_file)
        ctx["hostname"] = _get_hostname()
        return ctx


class JobCreateView(LoginRequiredMixin, View):
    def post(self, request):
        from .forms import RunStateForm
        form = RunStateForm(request.POST)
        if not form.is_valid():
            messages.error(request, f"Invalid form: {form.errors.as_text()}")
            return redirect("seeder")

        state_codes = form.cleaned_data["state_codes"]
        phases = form.cleaned_data["phases"]
        config = {k: v for k, v in form.cleaned_data.items() if k not in ("state_codes", "phases") and v not in (None, "", False)}
        config = _expand_llm_preset(config)

        try:
            jobs = create_state_jobs(state_codes, phases, config, _get_hostname())
            _log_audit(request, "job_create", "state_jobs", {
                "states": state_codes, "phases": phases, "job_ids": [j.pk for j in jobs]
            })
            messages.success(request, f"Created {len(jobs)} job(s)")
        except DuplicateJobError as e:
            messages.error(request, str(e))
        except InvalidParameterError as e:
            messages.error(request, str(e))

        return redirect("seeder")


class CrawlJobCreateView(LoginRequiredMixin, View):
    def post(self, request):
        from .forms import RunCrawlForm
        form = RunCrawlForm(request.POST)
        if not form.is_valid():
            messages.error(request, f"Invalid form: {form.errors.as_text()}")
            return redirect("crawler")

        config = {k: v for k, v in form.cleaned_data.items() if v not in (None, "", False)}
        if "async_mode" in config:
            config["async"] = config.pop("async_mode")
        try:
            job = create_crawl_job(config, _get_hostname())
            _log_audit(request, "job_create", "crawl", {"job_id": job.pk})
            messages.success(request, f"Created crawl job #{job.pk}")
        except DuplicateJobError as e:
            messages.error(request, str(e))

        return redirect("crawler")


class ResolveJobCreateView(LoginRequiredMixin, View):
    def post(self, request):
        from .forms import ResolverForm
        form = ResolverForm(request.POST)
        if not form.is_valid():
            messages.error(request, f"Invalid form: {form.errors.as_text()}")
            return redirect("resolver")

        config = {k: v for k, v in form.cleaned_data.items() if v not in (None, "", False)}
        config = _expand_llm_preset(config)
        try:
            job = create_resolve_job(config, _get_hostname())
            _log_audit(request, "job_create", "resolve", {"job_id": job.pk})
            messages.success(request, f"Created resolve job #{job.pk}")
        except DuplicateJobError as e:
            messages.error(request, str(e))

        return redirect("resolver")


class ClassifyJobCreateView(LoginRequiredMixin, View):
    def post(self, request):
        from .forms import ClassifierForm
        form = ClassifierForm(request.POST)
        if not form.is_valid():
            messages.error(request, f"Invalid form: {form.errors.as_text()}")
            return redirect("classifier")

        config = {k: v for k, v in form.cleaned_data.items() if v not in (None, "", False)}
        config = _expand_llm_preset(config)
        try:
            job = create_classify_job(config, _get_hostname())
            _log_audit(request, "job_create", "classify", {"job_id": job.pk})
            messages.success(request, f"Created classify job #{job.pk}")
        except DuplicateJobError as e:
            messages.error(request, str(e))

        return redirect("classifier")


class JobCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        cancel_job(job)
        _log_audit(request, "job_cancel", job.phase, {"job_id": job.pk})
        messages.success(request, f"Cancelled job #{job.pk}")
        return redirect("job_detail", pk=pk)


class JobRetryView(LoginRequiredMixin, View):
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        try:
            new_job = retry_job(job)
            _log_audit(request, "job_retry", job.phase, {"old_job_id": job.pk, "new_job_id": new_job.pk})
            messages.success(request, f"Created retry job #{new_job.pk}")
            return redirect("job_detail", pk=new_job.pk)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("job_detail", pk=pk)


class JobProgressPartial(HtmxLoginRequiredMixin, TemplateView):
    template_name = "pipeline/partials/job_progress.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        job = get_object_or_404(Job, pk=self.kwargs["pk"])
        ctx["job"] = job
        ctx["progress"] = _get_job_progress(job)
        return ctx


class JobLogPartial(HtmxLoginRequiredMixin, View):
    def get(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        content = read_log_tail(job.log_file)
        from django.http import HttpResponse
        return HttpResponse(
            f'<pre class="text-xs bg-gray-900 text-green-400 p-4 rounded overflow-auto max-h-96">{_escape(content)}</pre>'
        )


def _escape(text):
    """HTML-escape text for safe rendering."""
    from django.utils.html import escape
    return escape(text)


def _get_job_progress(job):
    """Get live progress for a job based on its phase."""
    if job.status != "running":
        return {"current": job.progress_current, "total": job.progress_total}

    if job.phase == "seed":
        current = NonprofitSeed.objects.filter(state=job.state_code).count() if job.state_code else 0
        return {"current": current, "total": None}

    if job.phase == "resolve" and job.started_at and job.state_code:
        current = NonprofitSeed.objects.filter(
            state=job.state_code,
            resolver_updated_at__gte=job.started_at,
        ).count()
        total = job.progress_total
        return {"current": current, "total": total}

    if job.phase == "crawl":
        current = CrawledOrg.objects.count()
        total = job.progress_total
        return {"current": current, "total": total}

    if job.phase == "classify":
        current = Report.objects.filter(classification__isnull=False).count()
        total = job.progress_total
        return {"current": current, "total": total}

    return {"current": job.progress_current, "total": job.progress_total}


# ---------------------------------------------------------------------------
# Pipeline Controls
# ---------------------------------------------------------------------------

class ResolverView(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/resolver.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["running_job"] = Job.objects.filter(phase="resolve", status="running").first()
        ctx["pending_job"] = Job.objects.filter(phase="resolve", status="pending").first()
        ctx["recent_results"] = NonprofitSeed.objects.filter(
            resolver_updated_at__isnull=False
        ).order_by("-resolver_updated_at")[:50]
        from .forms import ResolverForm
        ctx["form"] = ResolverForm()
        return ctx


class CrawlerView(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/crawler.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        try:
            ctx["process"] = check_process("crawl")
        except PipelineProcess.DoesNotExist:
            ctx["process"] = None
        ctx["running_job"] = Job.objects.filter(phase="crawl", status="running").first()
        ctx["recent_orgs"] = CrawledOrg.objects.order_by("-last_crawled_at")[:50]
        ctx["recent_reports"] = Report.objects.order_by("-archived_at")[:50]
        from .forms import CrawlerForm, RunCrawlForm
        ctx["form"] = CrawlerForm()
        ctx["crawl_job_form"] = RunCrawlForm()
        return ctx


class ClassifierView(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/classifier.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        try:
            ctx["process"] = check_process("classify")
        except PipelineProcess.DoesNotExist:
            ctx["process"] = None
        ctx["running_job"] = Job.objects.filter(phase="classify", status="running").first()
        ctx["recent_results"] = Report.objects.filter(
            classification__isnull=False
        ).order_by("-archived_at")[:50]
        from .forms import ClassifierForm
        ctx["form"] = ClassifierForm()
        return ctx


class ProcessStartView(LoginRequiredMixin, View):
    def post(self, request, phase):
        form_map = {
            "resolve": "ResolverForm",
            "crawl": "CrawlerForm",
            "classify": "ClassifierForm",
        }
        redirect_map = {
            "resolve": "resolver",
            "crawl": "crawler",
            "classify": "classifier",
        }
        if phase not in form_map:
            messages.error(request, f"Unknown phase: {phase}")
            return redirect("dashboard")

        from . import forms
        form_cls = getattr(forms, form_map[phase])
        form = form_cls(request.POST)
        if not form.is_valid():
            messages.error(request, f"Invalid form: {form.errors.as_text()}")
            return redirect(redirect_map[phase])

        config = {k: v for k, v in form.cleaned_data.items() if v not in (None, "", False)}
        try:
            start_process(phase, config)
            _log_audit(request, "start", phase, config)
            messages.success(request, f"Started {phase}")
        except RuntimeError as e:
            messages.error(request, str(e))

        return redirect(redirect_map[phase])


class ProcessStopView(LoginRequiredMixin, View):
    def post(self, request, phase):
        redirect_map = {
            "resolve": "resolver",
            "crawl": "crawler",
            "classify": "classifier",
        }
        try:
            stop_process(phase)
            _log_audit(request, "stop", phase)
            messages.success(request, f"Stopped {phase}")
        except Exception as e:
            messages.error(request, str(e))

        return redirect(redirect_map.get(phase, "dashboard"))


# ---------------------------------------------------------------------------
# Org Browser
# ---------------------------------------------------------------------------

class OrgListView(LoginRequiredMixin, ListView):
    model = NonprofitSeed
    template_name = "pipeline/orgs.html"
    context_object_name = "orgs"
    paginate_by = 50

    def get_queryset(self):
        qs = NonprofitSeed.objects.all().order_by("ein")
        state = self.request.GET.get("state")
        status = self.request.GET.get("resolver_status")
        method = self.request.GET.get("resolver_method")
        if state:
            qs = qs.filter(state=state)
        if status:
            qs = qs.filter(resolver_status=status)
        if method:
            qs = qs.filter(resolver_method=method)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_state"] = self.request.GET.get("state", "")
        ctx["filter_status"] = self.request.GET.get("resolver_status", "")
        ctx["filter_method"] = self.request.GET.get("resolver_method", "")
        return ctx


class OrgDetailView(LoginRequiredMixin, DetailView):
    model = NonprofitSeed
    template_name = "pipeline/org_detail.html"
    context_object_name = "org"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ein = self.object.ein

        filings = FilingIndex.objects.using("pipeline").filter(
            ein=ein
        ).order_by("-tax_period", "-object_id")
        ctx["filings"] = filings

        selected_oid = self.request.GET.get("filing")
        selected = None
        if selected_oid:
            selected = filings.filter(object_id=selected_oid).first()
        if selected is None:
            selected = filings.first()
        ctx["selected_filing"] = selected

        if selected:
            people_qs = Person.objects.using("pipeline").filter(
                ein=ein, object_id=selected.object_id
            )
            ctx["officers"] = people_qs.exclude(
                person_type="contractor"
            ).order_by("-reportable_comp", "person_name")
            ctx["contractors"] = people_qs.filter(
                person_type="contractor"
            ).order_by("-reportable_comp")
            ctx["schedule_j"] = people_qs.filter(
                total_comp_sch_j__isnull=False
            ).order_by("-total_comp_sch_j")

        if filings.count() > 1:
            all_people = Person.objects.using("pipeline").filter(ein=ein)
            comparison = {}
            for p in all_people:
                row = comparison.setdefault(p.person_name, {})
                existing = row.get(p.object_id)
                if existing is None or (p.reportable_comp or 0) > (existing or 0):
                    row[p.object_id] = p.reportable_comp
            ctx["comparison"] = comparison
            tp_counts = {}
            for f in filings:
                tp_counts[f.tax_period] = tp_counts.get(f.tax_period, 0) + 1
            ctx["filing_headers"] = [
                {
                    "object_id": f.object_id,
                    "label": f.tax_period + (
                        " (amended)" if f.is_amended or tp_counts[f.tax_period] > 1
                        else ""
                    ),
                }
                for f in filings
            ]

        return ctx


# ---------------------------------------------------------------------------
# 990 Pipeline Controls
# ---------------------------------------------------------------------------

def _build_990_status_qs(request, form_cls):
    """Build scoped filing_index queryset from GET params, validated via form."""
    scope_form = form_cls(request.GET)
    if scope_form.is_valid():
        state = scope_form.cleaned_data.get("state") or None
        ein = scope_form.cleaned_data.get("ein") or None
        years_param = scope_form.cleaned_data.get("years") or None
    else:
        state = ein = years_param = None

    qs = FilingIndex.objects.using("pipeline").all()
    scoped = bool(state or ein or years_param)
    scope_truncated = False

    if scoped:
        if ein:
            qs = qs.filter(ein=ein)
        elif state:
            ein_list = list(
                NonprofitSeed.objects.filter(state=state)
                .values_list("ein", flat=True)[:10000]
            )
            if len(ein_list) >= 10000:
                scope_truncated = True
            qs = qs.filter(ein__in=ein_list)
        if years_param:
            year_list = [int(y) for y in years_param.split(",") if y.strip().isdigit()]
            qs = qs.filter(filing_year__in=year_list)

    return qs, scoped, scope_truncated


class EnrichIndexView(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/990_index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["running_job"] = Job.objects.filter(phase="990-index", status="running").first()
        ctx["pending_job"] = Job.objects.filter(phase="990-index", status="pending").first()
        from .forms import EnrichIndexForm
        ctx["form"] = EnrichIndexForm()

        qs, scoped, scope_truncated = _build_990_status_qs(self.request, EnrichIndexForm)
        ctx["status_counts"] = list(qs.values("status").annotate(count=Count("status")))
        ctx["total_filings"] = qs.count()
        ctx["scoped"] = scoped
        ctx["scope_truncated"] = scope_truncated
        return ctx


class EnrichIndexJobCreateView(LoginRequiredMixin, View):
    def post(self, request):
        from .forms import EnrichIndexForm
        form = EnrichIndexForm(request.POST)
        if not form.is_valid():
            messages.error(request, f"Invalid form: {form.errors.as_text()}")
            return redirect("enrich_index")

        config = {k: v for k, v in form.cleaned_data.items() if v not in (None, "", False)}
        try:
            job = create_990_index_job(config, _get_hostname())
            _log_audit(request, "job_create", "990-index", {"job_id": job.pk})
            messages.success(request, f"Created 990 index job #{job.pk}")
        except DuplicateJobError as e:
            messages.error(request, str(e))

        params = {}
        if config.get("ein"):
            params["ein"] = config["ein"]
        elif config.get("state"):
            params["state"] = config["state"]
        if config.get("years"):
            params["years"] = config["years"]
        if params:
            from urllib.parse import urlencode
            return redirect(f"{reverse('enrich_index')}?{urlencode(params)}")
        return redirect("enrich_index")


class EnrichParseView(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/990_parse.html"

    def get_context_data(self, **kwargs):
        from pathlib import Path

        ctx = super().get_context_data(**kwargs)
        ctx["running_job"] = Job.objects.filter(phase="990-parse", status="running").first()
        ctx["pending_job"] = Job.objects.filter(phase="990-parse", status="pending").first()
        from .forms import EnrichParseForm
        ctx["form"] = EnrichParseForm()

        qs, scoped, scope_truncated = _build_990_status_qs(self.request, EnrichParseForm)
        ctx["status_counts"] = list(qs.values("status").annotate(count=Count("status")))
        ctx["total_filings"] = qs.count()
        ctx["scoped"] = scoped
        ctx["scope_truncated"] = scope_truncated

        # People count (scoped same way)
        people_qs = Person.objects.using("pipeline").all()
        scope_form = EnrichParseForm(self.request.GET)
        if scope_form.is_valid():
            ein = scope_form.cleaned_data.get("ein") or None
            state = scope_form.cleaned_data.get("state") or None
            years_param = scope_form.cleaned_data.get("years") or None
        else:
            ein = state = years_param = None

        if ein:
            people_qs = people_qs.filter(ein=ein)
        elif state:
            ein_list = list(
                NonprofitSeed.objects.filter(state=state)
                .values_list("ein", flat=True)[:10000]
            )
            if len(ein_list) >= 10000:
                ctx["scope_truncated"] = True
            people_qs = people_qs.filter(ein__in=ein_list)
        if years_param:
            year_list = [y.strip() for y in years_param.split(",") if y.strip().isdigit()]
            year_q = Q()
            for y in year_list:
                year_q |= Q(tax_period__startswith=y)
            if year_q:
                people_qs = people_qs.filter(year_q)
        ctx["people_count"] = people_qs.count()

        # Cache status
        cache_dir = Path.home() / ".lavandula" / "990-cache"
        try:
            zips = list(cache_dir.glob("*.zip"))
            ctx["cache_count"] = len(zips)
            ctx["cache_size_gb"] = sum(f.stat().st_size for f in zips) / (1024 ** 3)
        except (OSError, PermissionError):
            ctx["cache_count"] = None

        return ctx


class EnrichParseJobCreateView(LoginRequiredMixin, View):
    def post(self, request):
        from .forms import EnrichParseForm
        form = EnrichParseForm(request.POST)
        if not form.is_valid():
            messages.error(request, f"Invalid form: {form.errors.as_text()}")
            return redirect("enrich_parse")

        config = {k: v for k, v in form.cleaned_data.items() if v not in (None, "", False)}
        try:
            job = create_990_parse_job(config, _get_hostname())
            _log_audit(request, "job_create", "990-parse", {"job_id": job.pk})
            messages.success(request, f"Created 990 parse job #{job.pk}")
        except DuplicateJobError as e:
            messages.error(request, str(e))

        params = {}
        if config.get("ein"):
            params["ein"] = config["ein"]
        elif config.get("state"):
            params["state"] = config["state"]
        if config.get("years"):
            params["years"] = config["years"]
        if params:
            from urllib.parse import urlencode
            return redirect(f"{reverse('enrich_parse')}?{urlencode(params)}")
        return redirect("enrich_parse")


# ---------------------------------------------------------------------------
# Reports Browser
# ---------------------------------------------------------------------------

class ReportListView(LoginRequiredMixin, ListView):
    model = Report
    template_name = "pipeline/reports.html"
    context_object_name = "reports"
    paginate_by = 50

    def get_queryset(self):
        qs = Report.objects.all().order_by("-archived_at")
        org = self.request.GET.get("org")
        material_type = self.request.GET.get("material_type")
        year = self.request.GET.get("report_year")
        date_from = self.request.GET.get("date_from")
        date_to = self.request.GET.get("date_to")
        if org:
            matching_eins = NonprofitSeed.objects.filter(
                Q(ein__icontains=org) | Q(name__icontains=org)
            ).values_list("ein", flat=True)[:1000]
            qs = qs.filter(source_org_ein__in=matching_eins)
        if material_type:
            qs = qs.filter(material_type=material_type)
        if year:
            try:
                qs = qs.filter(report_year=int(year))
            except ValueError:
                pass
        if date_from:
            qs = qs.filter(archived_at__gte=date_from)
        if date_to:
            qs = qs.filter(archived_at__lte=date_to + "T23:59:59")
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_org"] = self.request.GET.get("org", "")
        ctx["filter_material_type"] = self.request.GET.get("material_type", "")
        ctx["filter_year"] = self.request.GET.get("report_year", "")
        ctx["filter_date_from"] = self.request.GET.get("date_from", "")
        ctx["filter_date_to"] = self.request.GET.get("date_to", "")
        ctx["material_type_choices"] = (
            Report.objects.filter(material_type__isnull=False)
            .values_list("material_type", flat=True)
            .distinct()
            .order_by("material_type")
        )
        return ctx


class ReportDetailView(LoginRequiredMixin, DetailView):
    model = Report
    template_name = "pipeline/report_detail.html"
    context_object_name = "report"
    slug_field = "content_sha256"
    slug_url_kwarg = "sha"


class ReportDownloadView(LoginRequiredMixin, View):
    def get(self, request, sha):
        report = get_object_or_404(Report, content_sha256=sha)

        import boto3
        from django.conf import settings

        s3 = boto3.client("s3")
        key = f"pdfs/{report.content_sha256}.pdf"
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_COLLATERAL_BUCKET, "Key": key},
            ExpiresIn=300,
        )
        return HttpResponseRedirect(url)

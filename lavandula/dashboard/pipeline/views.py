import socket

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from .models import (
    CrawledOrg,
    Job,
    NonprofitSeed,
    PipelineAuditLog,
    PipelineProcess,
    Report,
)
from .orchestrator import (
    DuplicateJobError,
    InvalidParameterError,
    cancel_job,
    create_classify_job,
    create_crawl_job,
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


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(_dashboard_stats())
        return ctx


class DashboardStatsPartial(LoginRequiredMixin, TemplateView):
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

class JobListView(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/jobs.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_jobs"] = Job.objects.filter(status="running").order_by("-started_at")
        ctx["pending_jobs"] = Job.objects.filter(status="pending").order_by("created_at")
        ctx["history_jobs"] = Job.objects.filter(
            status__in=["completed", "failed", "cancelled"]
        ).order_by("-finished_at")[:50]
        from .forms import RunStateForm
        ctx["form"] = RunStateForm()
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
            return redirect("job_list")

        state_codes = form.cleaned_data["state_codes"]
        phases = form.cleaned_data["phases"]
        config = {k: v for k, v in form.cleaned_data.items() if k not in ("state_codes", "phases") and v not in (None, "")}

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

        return redirect("job_list")


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


class ClassifyJobCreateView(LoginRequiredMixin, View):
    def post(self, request):
        from .forms import ClassifierForm
        form = ClassifierForm(request.POST)
        if not form.is_valid():
            messages.error(request, f"Invalid form: {form.errors.as_text()}")
            return redirect("classifier")

        config = {k: v for k, v in form.cleaned_data.items() if v not in (None, "", False)}
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


class JobProgressPartial(LoginRequiredMixin, TemplateView):
    template_name = "pipeline/partials/job_progress.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        job = get_object_or_404(Job, pk=self.kwargs["pk"])
        ctx["job"] = job
        ctx["progress"] = _get_job_progress(job)
        return ctx


class JobLogPartial(LoginRequiredMixin, View):
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
        try:
            ctx["process"] = check_process("resolve")
        except PipelineProcess.DoesNotExist:
            ctx["process"] = None
        ctx["running_job"] = Job.objects.filter(phase="resolve", status="running").first()
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

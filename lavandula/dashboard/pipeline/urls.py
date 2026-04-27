from django.urls import path

from . import views

urlpatterns = [
    # Dashboard
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("stats/", views.DashboardStatsPartial.as_view(), name="dashboard_stats"),

    # Jobs
    path("jobs/", views.JobListView.as_view(), name="job_list"),
    path("jobs/create/", views.JobCreateView.as_view(), name="job_create"),
    path("jobs/<int:pk>/", views.JobDetailView.as_view(), name="job_detail"),
    path("jobs/<int:pk>/cancel/", views.JobCancelView.as_view(), name="job_cancel"),
    path("jobs/<int:pk>/retry/", views.JobRetryView.as_view(), name="job_retry"),
    path("jobs/<int:pk>/progress/", views.JobProgressPartial.as_view(), name="job_progress"),
    path("jobs/<int:pk>/log/", views.JobLogPartial.as_view(), name="job_log"),

    # Pipeline Controls
    path("resolver/", views.ResolverView.as_view(), name="resolver"),
    path("resolver/queue/", views.ResolveJobCreateView.as_view(), name="resolve_job_create"),
    path("crawler/", views.CrawlerView.as_view(), name="crawler"),
    path("crawler/queue/", views.CrawlJobCreateView.as_view(), name="crawl_job_create"),
    path("classifier/", views.ClassifierView.as_view(), name="classifier"),
    path("classifier/queue/", views.ClassifyJobCreateView.as_view(), name="classify_job_create"),
    path("process/<str:phase>/start/", views.ProcessStartView.as_view(), name="process_start"),
    path("process/<str:phase>/stop/", views.ProcessStopView.as_view(), name="process_stop"),

    # Org Browser
    path("orgs/", views.OrgListView.as_view(), name="org_list"),
    path("orgs/<str:pk>/", views.OrgDetailView.as_view(), name="org_detail"),

    # Reports Browser
    path("reports/", views.ReportListView.as_view(), name="report_list"),
    path("reports/<str:sha>/", views.ReportDetailView.as_view(), name="report_detail"),
    path("reports/<str:sha>/download/", views.ReportDownloadView.as_view(), name="report_download"),
]

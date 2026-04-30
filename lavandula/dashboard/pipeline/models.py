from django.db import models


# ---------------------------------------------------------------------------
# Unmanaged models — read-only views of existing lava_corpus tables
# ---------------------------------------------------------------------------

class NonprofitSeed(models.Model):
    ein = models.TextField(primary_key=True)
    name = models.TextField(null=True)
    city = models.TextField(null=True)
    state = models.TextField(null=True)
    website_url = models.TextField(null=True)
    website_candidates_json = models.TextField(null=True)
    resolver_status = models.TextField(null=True)
    resolver_confidence = models.FloatField(null=True)
    resolver_method = models.TextField(null=True)
    resolver_reason = models.TextField(null=True)
    resolver_updated_at = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = "nonprofits_seed"


class Report(models.Model):
    content_sha256 = models.TextField(primary_key=True)
    source_org_ein = models.TextField()
    source_url_redacted = models.TextField(null=True)
    classification = models.TextField(null=True)
    classification_confidence = models.FloatField(null=True)
    material_type = models.TextField(null=True)
    material_group = models.TextField(null=True)
    archived_at = models.TextField()
    file_size_bytes = models.BigIntegerField()
    page_count = models.IntegerField(null=True)
    report_year = models.IntegerField(null=True)
    first_page_text = models.TextField(null=True)

    class Meta:
        managed = False
        db_table = "corpus"


class CrawledOrg(models.Model):
    ein = models.TextField(primary_key=True)
    first_crawled_at = models.TextField()
    last_crawled_at = models.TextField()
    candidate_count = models.IntegerField()
    fetched_count = models.IntegerField()
    confirmed_report_count = models.IntegerField()

    class Meta:
        managed = False
        db_table = "crawled_orgs"


# ---------------------------------------------------------------------------
# Managed models — Django-managed tables in lava_dashboard schema
# ---------------------------------------------------------------------------

class Job(models.Model):
    PHASE_CHOICES = [
        ("seed", "Seed"),
        ("resolve", "Resolve"),
        ("crawl", "Crawl"),
        ("classify", "Classify"),
        ("990-enrich", "990 Enrich"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]

    state_code = models.CharField(max_length=2, null=True, blank=True)
    phase = models.CharField(max_length=20, choices=PHASE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    host = models.CharField(max_length=100, default="localhost")
    pid = models.IntegerField(null=True, blank=True)
    config_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    log_file = models.CharField(max_length=255, null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    progress_current = models.IntegerField(default=0)
    progress_total = models.IntegerField(null=True, blank=True)
    depends_on = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="dependents"
    )
    last_heartbeat = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "jobs"
        indexes = [
            models.Index(fields=["status", "phase"]),
            models.Index(fields=["state_code", "phase"]),
        ]

    def __str__(self):
        state = self.state_code or "global"
        return f"Job {self.pk}: {self.phase} ({state}) [{self.status}]"


class PipelineProcess(models.Model):
    STATUS_CHOICES = [
        ("running", "Running"),
        ("stopped", "Stopped"),
        ("error", "Error"),
    ]

    name = models.CharField(max_length=50, unique=True)
    pid = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="stopped")
    started_at = models.DateTimeField(null=True, blank=True)
    config_json = models.JSONField(default=dict, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    log_file = models.CharField(max_length=255, null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "pipeline_processes"

    def __str__(self):
        return f"{self.name} [{self.status}]"


class PipelineAuditLog(models.Model):
    action = models.CharField(max_length=20)
    process_name = models.CharField(max_length=50)
    parameters = models.JSONField(default=dict, blank=True)
    source_ip = models.GenericIPAddressField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pipeline_audit_log"
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.action} {self.process_name} @ {self.timestamp}"

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
    phone = models.TextField(null=True)
    phone_source = models.TextField(null=True)

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


class FilingIndex(models.Model):
    STATUS_CHOICES = [
        ("indexed", "Indexed"),
        ("downloaded", "Downloaded"),
        ("parsed", "Parsed"),
        ("error", "Error"),
        ("batch_unresolvable", "Batch Unresolvable"),
        ("skipped", "Skipped"),
    ]

    object_id = models.CharField(primary_key=True, max_length=30)
    ein = models.CharField(max_length=9)
    tax_period = models.CharField(max_length=6)
    return_type = models.CharField(max_length=10)
    sub_date = models.CharField(max_length=20, null=True)
    return_ts = models.DateTimeField(null=True)
    is_amended = models.BooleanField(default=False)
    taxpayer_name = models.TextField(null=True)
    xml_batch_id = models.CharField(max_length=30, null=True)
    filing_year = models.IntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    error_message = models.TextField(null=True)
    parsed_at = models.DateTimeField(null=True)
    run_id = models.CharField(max_length=50, null=True)
    first_indexed_at = models.DateTimeField(null=True)
    last_seen_at = models.DateTimeField(null=True)
    s3_xml_key = models.TextField(null=True)
    zip_checksum = models.TextField(null=True)

    class Meta:
        managed = False
        db_table = "filing_index"

    def __str__(self):
        return f"{self.object_id} ({self.ein} {self.tax_period})"


class IndexRefreshLog(models.Model):
    filing_year = models.IntegerField()
    refreshed_at = models.DateTimeField()
    rows_scanned = models.IntegerField(default=0)
    rows_inserted = models.IntegerField(default=0)
    rows_skipped = models.IntegerField(default=0)
    duration_sec = models.DecimalField(max_digits=8, decimal_places=2, null=True)

    class Meta:
        managed = False
        db_table = "index_refresh_log"

    def __str__(self):
        return f"Refresh {self.filing_year} @ {self.refreshed_at}"


class Person(models.Model):
    id = models.AutoField(primary_key=True)
    ein = models.CharField(max_length=9)
    tax_period = models.CharField(max_length=6)
    object_id = models.CharField(max_length=30)
    person_name = models.CharField(max_length=200)
    title = models.TextField(null=True)
    person_type = models.CharField(max_length=30)
    avg_hours_per_week = models.DecimalField(max_digits=5, decimal_places=1, null=True)
    reportable_comp = models.BigIntegerField(null=True)
    related_org_comp = models.BigIntegerField(null=True)
    other_comp = models.BigIntegerField(null=True)
    total_comp = models.BigIntegerField(null=True)
    base_comp = models.BigIntegerField(null=True)
    bonus = models.BigIntegerField(null=True)
    other_reportable = models.BigIntegerField(null=True)
    deferred_comp = models.BigIntegerField(null=True)
    nontaxable_benefits = models.BigIntegerField(null=True)
    total_comp_sch_j = models.BigIntegerField(null=True)
    services_desc = models.TextField(null=True)
    is_officer = models.BooleanField(default=False)
    is_director = models.BooleanField(default=False)
    is_key_employee = models.BooleanField(default=False)
    is_highest_comp = models.BooleanField(default=False)
    is_former = models.BooleanField(default=False)
    extracted_at = models.DateTimeField(null=True)
    run_id = models.CharField(max_length=50, null=True)

    class Meta:
        managed = False
        db_table = "people"

    def __str__(self):
        return f"{self.person_name} ({self.ein} {self.person_type})"


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
        ("990-index", "990 Index"),
        ("990-parse", "990 Parse"),
        ("enrich-phone", "Phone Enrich"),
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

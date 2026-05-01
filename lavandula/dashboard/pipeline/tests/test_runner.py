"""Custom test runner that creates tables for unmanaged models in the test DB."""
from django.test.runner import DiscoverRunner


_UNMANAGED_TABLES = [
    """
        CREATE TABLE IF NOT EXISTS nonprofits_seed (
            ein TEXT PRIMARY KEY,
            name TEXT,
            city TEXT,
            state TEXT,
            website_url TEXT,
            website_candidates_json TEXT,
            resolver_status TEXT,
            resolver_confidence REAL,
            resolver_method TEXT,
            resolver_reason TEXT,
            resolver_updated_at TEXT
        )
    """,
    """
        CREATE TABLE IF NOT EXISTS corpus (
            content_sha256 TEXT PRIMARY KEY,
            source_org_ein TEXT NOT NULL,
            source_url_redacted TEXT,
            classification TEXT,
            classification_confidence REAL,
            material_type TEXT,
            material_group TEXT,
            archived_at TEXT NOT NULL,
            file_size_bytes INTEGER NOT NULL,
            page_count INTEGER,
            report_year INTEGER,
            first_page_text TEXT
        )
    """,
    """
        CREATE TABLE IF NOT EXISTS crawled_orgs (
            ein TEXT PRIMARY KEY,
            first_crawled_at TEXT NOT NULL,
            last_crawled_at TEXT NOT NULL,
            candidate_count INTEGER NOT NULL,
            fetched_count INTEGER NOT NULL,
            confirmed_report_count INTEGER NOT NULL
        )
    """,
    """
        CREATE TABLE IF NOT EXISTS filing_index (
            object_id TEXT PRIMARY KEY,
            ein TEXT NOT NULL,
            tax_period TEXT NOT NULL,
            return_type TEXT NOT NULL,
            sub_date TEXT,
            return_ts TEXT,
            is_amended INTEGER DEFAULT 0,
            taxpayer_name TEXT,
            xml_batch_id TEXT,
            filing_year INTEGER NOT NULL,
            status TEXT DEFAULT 'indexed',
            error_message TEXT,
            parsed_at TEXT,
            run_id TEXT
        )
    """,
    """
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ein TEXT NOT NULL,
            tax_period TEXT NOT NULL,
            object_id TEXT NOT NULL,
            person_name TEXT NOT NULL,
            title TEXT,
            person_type TEXT NOT NULL,
            avg_hours_per_week REAL,
            reportable_comp INTEGER,
            related_org_comp INTEGER,
            other_comp INTEGER,
            total_comp INTEGER,
            base_comp INTEGER,
            bonus INTEGER,
            other_reportable INTEGER,
            deferred_comp INTEGER,
            nontaxable_benefits INTEGER,
            total_comp_sch_j INTEGER,
            services_desc TEXT,
            is_officer INTEGER DEFAULT 0,
            is_director INTEGER DEFAULT 0,
            is_key_employee INTEGER DEFAULT 0,
            is_highest_comp INTEGER DEFAULT 0,
            is_former INTEGER DEFAULT 0,
            extracted_at TEXT,
            run_id TEXT
        )
    """,
]


class UnmanagedModelTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        result = super().setup_databases(**kwargs)
        from django.db import connections

        for alias in connections:
            try:
                conn = connections[alias]
                with conn.cursor() as cursor:
                    for sql in _UNMANAGED_TABLES:
                        cursor.execute(sql)
            except Exception:
                pass
        return result

"""Custom test runner that creates tables for unmanaged models in the test DB."""
from django.test.runner import DiscoverRunner


class UnmanagedModelTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        result = super().setup_databases(**kwargs)
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("""
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
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    content_sha256 TEXT PRIMARY KEY,
                    source_org_ein TEXT NOT NULL,
                    source_url_redacted TEXT,
                    classification TEXT,
                    classification_confidence REAL,
                    archived_at TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL,
                    page_count INTEGER,
                    report_year INTEGER,
                    first_page_text TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS crawled_orgs (
                    ein TEXT PRIMARY KEY,
                    first_crawled_at TEXT NOT NULL,
                    last_crawled_at TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    fetched_count INTEGER NOT NULL,
                    confirmed_report_count INTEGER NOT NULL
                )
            """)
        return result

"""Spec 0007 — unit tests for S3Archive (moto-backed).

Covers ACs: 1, 2, 3, 5, 7 (startup probe variants), 13, 14.
"""
from __future__ import annotations

import re

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from moto import mock_aws

from lavandula.reports import s3_archive as s3a


BUCKET = "test-lavandula-archive"
REGION = "us-east-1"
PDF_BYTES = b"%PDF-1.4\n% minimal\n%%EOF\n"
SHA = "a" * 64


@pytest.fixture
def s3_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")


@pytest.fixture
def moto_s3(s3_env):
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


def _archive(client, prefix="pdfs"):
    return s3a.S3Archive(BUCKET, prefix, region=REGION, client=client)


# ---------------------------------------------------------------------
# AC1 — key format + in-memory only write
# ---------------------------------------------------------------------

def test_ac1_key_format(moto_s3):
    arch = _archive(moto_s3)
    arch.put(SHA, PDF_BYTES, {"ein": "123456789"})
    resp = moto_s3.list_objects_v2(Bucket=BUCKET)
    keys = [o["Key"] for o in resp.get("Contents", [])]
    assert keys == [f"pdfs/{SHA}.pdf"]


def test_ac1_empty_prefix_uses_flat_key(moto_s3):
    arch = _archive(moto_s3, prefix="")
    arch.put(SHA, PDF_BYTES, {})
    resp = moto_s3.list_objects_v2(Bucket=BUCKET)
    assert resp["Contents"][0]["Key"] == f"{SHA}.pdf"


# ---------------------------------------------------------------------
# AC2 — SSE AES256
# ---------------------------------------------------------------------

def test_ac2_sse_aes256_applied(moto_s3):
    arch = _archive(moto_s3)
    arch.put(SHA, PDF_BYTES, {})
    head = moto_s3.head_object(Bucket=BUCKET, Key=f"pdfs/{SHA}.pdf")
    assert head.get("ServerSideEncryption") == "AES256"


# ---------------------------------------------------------------------
# AC3 — canonical metadata keys
# ---------------------------------------------------------------------

def test_ac3_canonical_metadata_keys(moto_s3):
    arch = _archive(moto_s3)
    metadata = {
        "source-url": "https://example.org/report.pdf",
        "ein": "123456789",
        "crawl-run-id": "deadbeef",
        "fetched-at": "2026-04-22T16:30:05Z",
        "attribution-confidence": "own_domain",
        "discovered-via": "homepage-link",
    }
    arch.put(SHA, PDF_BYTES, metadata)
    head = moto_s3.head_object(Bucket=BUCKET, Key=f"pdfs/{SHA}.pdf")
    md = head["Metadata"]
    # Round-4: adds attribution-confidence and discovered-via so
    # reconciled rows can land in the corpus_public view.
    assert set(md) == {
        "source-url", "ein", "crawl-run-id", "fetched-at",
        "attribution-confidence", "discovered-via",
    }
    assert md["ein"] == "123456789"
    assert md["crawl-run-id"] == "deadbeef"
    assert md["fetched-at"] == "2026-04-22T16:30:05Z"
    assert md["attribution-confidence"] == "own_domain"
    assert md["discovered-via"] == "homepage-link"
    # percent-encoded
    assert md["source-url"] == "https%3A%2F%2Fexample.org%2Freport.pdf"


# ---------------------------------------------------------------------
# AC5 — 50 MB cap
# ---------------------------------------------------------------------

def test_ac5_oversize_rejected(moto_s3, monkeypatch):
    monkeypatch.setattr(s3a.config, "MAX_PDF_BYTES", 16)
    arch = _archive(moto_s3)
    with pytest.raises(s3a.ArchiveSizeError):
        arch.put(SHA, b"x" * 17, {})


# ---------------------------------------------------------------------
# AC7 — startup probe variants
# ---------------------------------------------------------------------

def test_startup_probe_soft_region_from_head_bucket(s3_env):
    """Round-3 review: client region_name=None should NOT hard-fail if
    head_bucket returns x-amz-bucket-region. Soft success path."""
    client = boto3.client("s3", region_name=REGION)
    stubber = Stubber(client)
    stubber.add_response(
        "head_bucket",
        {"ResponseMetadata": {"HTTPHeaders": {"x-amz-bucket-region": "us-east-1"}}},
        {"Bucket": "b"},
    )
    # Lie about region_name: present as None via a shim.
    class _Meta:
        region_name = None
        events = client.meta.events

    class _Shim:
        meta = _Meta()

        def head_bucket(self, **kw):
            return client.head_bucket(**kw)

        def get_bucket_versioning(self, **kw):
            from botocore.exceptions import ClientError as CE
            raise CE({"Error": {"Code": "AccessDenied"}}, "GetBucketVersioning")

    with stubber:
        arch = s3a.S3Archive("b", "pdfs", client=_Shim())
        arch.startup_probe()  # does not raise


def test_startup_probe_raises_when_neither_region_source(s3_env):
    """If region_name is None AND head_bucket returns no region header
    on error, we still raise the 'no region' ArchiveSetupError."""
    client = boto3.client("s3", region_name=REGION)
    stubber = Stubber(client)
    stubber.add_client_error(
        "head_bucket",
        service_error_code="SomeOtherError",
        http_status_code=500,
    )
    class _Meta:
        region_name = None
        events = client.meta.events

    class _Shim:
        meta = _Meta()

        def head_bucket(self, **kw):
            return client.head_bucket(**kw)

    with stubber:
        arch = s3a.S3Archive("b", "pdfs", client=_Shim())
        with pytest.raises(s3a.ArchiveSetupError, match="no AWS region"):
            arch.startup_probe()


def test_ac7_bucket_not_found_raises(moto_s3):
    arch = _archive(moto_s3)
    arch.bucket = "no-such-bucket-0007"
    with pytest.raises(s3a.ArchiveSetupError, match="does not exist"):
        arch.startup_probe()


def test_ac7_access_denied_raises(s3_env):
    client = boto3.client("s3", region_name=REGION)
    stubber = Stubber(client)
    stubber.add_client_error(
        "head_bucket",
        service_error_code="403",
        http_status_code=403,
    )
    with stubber:
        arch = s3a.S3Archive("denied", "pdfs", region=REGION, client=client)
        with pytest.raises(s3a.ArchiveSetupError, match="no permission"):
            arch.startup_probe()


def test_ac7_region_mismatch_raises(s3_env):
    # Simulate a 301 with x-amz-bucket-region header pointing elsewhere.
    client = boto3.client("s3", region_name=REGION)
    stubber = Stubber(client)
    stubber.add_client_error(
        "head_bucket",
        service_error_code="PermanentRedirect",
        http_status_code=301,
        response_meta={"HTTPHeaders": {"x-amz-bucket-region": "us-west-2"}},
    )
    with stubber:
        arch = s3a.S3Archive("wrong", "pdfs", region=REGION, client=client)
        with pytest.raises(s3a.ArchiveSetupError, match="region us-west-2"):
            arch.startup_probe()


def test_ac7_happy_path_passes(moto_s3):
    arch = _archive(moto_s3)
    arch.startup_probe()  # no exception


# ---------------------------------------------------------------------
# AC13 — key basename regex
# ---------------------------------------------------------------------

def test_ac13_key_basename_regex(moto_s3):
    arch = _archive(moto_s3)
    arch.put(SHA, PDF_BYTES, {})
    resp = moto_s3.list_objects_v2(Bucket=BUCKET)
    key = resp["Contents"][0]["Key"]
    base = key.rsplit("/", 1)[-1]
    assert re.match(r"^[a-f0-9]{64}\.pdf$", base)
    assert re.match(r"^([^/]+/)*[a-f0-9]{64}\.pdf$", key)


def test_ac13_rejects_malformed_sha(moto_s3):
    arch = _archive(moto_s3)
    with pytest.raises(ValueError):
        arch.put("not-a-sha", PDF_BYTES, {})
    with pytest.raises(ValueError):
        arch.put("a" * 63, PDF_BYTES, {})  # too short


# ---------------------------------------------------------------------
# AC14 — never set ACL
# ---------------------------------------------------------------------

def test_ac14_put_never_sets_acl(s3_env):
    client = boto3.client("s3", region_name=REGION)
    stubber = Stubber(client)
    captured = {}

    expected = {
        "Bucket": BUCKET,
        "Key": f"pdfs/{SHA}.pdf",
        "Body": PDF_BYTES,
        "ContentLength": len(PDF_BYTES),
        "ContentType": "application/pdf",
        "ServerSideEncryption": "AES256",
        "Metadata": {},
    }

    def record(params, **kwargs):
        captured.update(params)

    stubber.add_response("put_object", {}, expected)
    with stubber:
        arch = s3a.S3Archive(BUCKET, "pdfs", region=REGION, client=client)
        arch.put(SHA, PDF_BYTES, {})
    # Stubber accepted the call only because our expected params
    # did not contain ACL — any attempt to pass ACL would have raised.
    # As a belt-and-braces assertion, re-run against a captured-params
    # event hook.

    call_params = {}

    def capture_before_param(params, **_):
        call_params.update(params)

    client2 = boto3.client("s3", region_name=REGION)
    client2.meta.events.register("before-parameter-build.s3.PutObject",
                                 capture_before_param)
    with mock_aws():
        client2.create_bucket(Bucket=BUCKET)
        arch2 = s3a.S3Archive(BUCKET, "pdfs", region=REGION, client=client2)
        arch2.put(SHA, PDF_BYTES, {})
    assert "ACL" not in call_params


# ---------------------------------------------------------------------
# get / head round-trip
# ---------------------------------------------------------------------

def test_get_and_head_roundtrip(moto_s3):
    arch = _archive(moto_s3)
    arch.put(SHA, PDF_BYTES, {"ein": "123456789"})
    assert arch.get(SHA) == PDF_BYTES
    h = arch.head(SHA)
    assert h is not None
    assert h["metadata"]["ein"] == "123456789"


def test_head_returns_none_when_missing(moto_s3):
    arch = _archive(moto_s3)
    assert arch.head(SHA) is None


# ---------------------------------------------------------------------
# AC8 — boto3 default retry exercised on transient 5xx
# ---------------------------------------------------------------------

def test_ac8_retry_config_is_pinned(s3_env):
    """Client factory pins standard-mode retries (not ambient defaults)."""
    arch = s3a.S3Archive("bucket", "pdfs", region=REGION)
    retries = arch._client.meta.config.retries  # noqa: SLF001
    assert retries["mode"] == "standard"
    # botocore exposes the resolved cap under `total_max_attempts`.
    # max_attempts=3 in Config resolves to total_max_attempts >= 3.
    assert retries.get("total_max_attempts", 0) >= 3


def test_ac8_503_then_success_retries(moto_s3):
    """Inject 503 on the first two PutObject attempts; third succeeds.

    Uses a before-send event hook (per plan Option A) so boto3's real
    retry stack exercises the standard exponential backoff.
    """
    import io
    import botocore.awsrequest

    class _FakeRaw(io.BytesIO):
        def stream(self, *_, **__):
            yield self.getvalue()

        def release_conn(self):
            pass

    arch = _archive(moto_s3)
    call_count = {"n": 0}

    def fake_503_then_success(request, **_):
        call_count["n"] += 1
        if call_count["n"] < 3:
            body = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b"<Error><Code>ServiceUnavailable</Code>"
                b"<Message>Slow down</Message></Error>"
            )
            return botocore.awsrequest.AWSResponse(
                url=request.url,
                status_code=503,
                headers={"Content-Type": "application/xml"},
                raw=_FakeRaw(body),
            )
        return None  # fall through to moto

    moto_s3.meta.events.register(
        "before-send.s3.PutObject", fake_503_then_success
    )
    try:
        arch.put(SHA, PDF_BYTES, {"ein": "123456789"})
    finally:
        moto_s3.meta.events.unregister(
            "before-send.s3.PutObject", fake_503_then_success
        )

    # Two injected 503s + one real success == 3 send attempts.
    assert call_count["n"] == 3
    head = moto_s3.head_object(Bucket=BUCKET, Key=f"pdfs/{SHA}.pdf")
    assert head["ServerSideEncryption"] == "AES256"


# ---------------------------------------------------------------------
# LAVANDULA_S3_ENDPOINT_URL override
# ---------------------------------------------------------------------

def test_endpoint_url_env_var_wired(monkeypatch):
    monkeypatch.setenv("LAVANDULA_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    arch = s3a.S3Archive("b", "pdfs", region=REGION)
    assert arch._endpoint_url == "http://localhost:9000"
    assert arch._client.meta.endpoint_url == "http://localhost:9000"


def test_endpoint_url_kwarg_overrides_env(monkeypatch):
    monkeypatch.setenv("LAVANDULA_S3_ENDPOINT_URL", "http://from-env:9000")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    arch = s3a.S3Archive(
        "b", "pdfs", region=REGION, endpoint_url="http://from-kwarg:9000"
    )
    assert arch._endpoint_url == "http://from-kwarg:9000"

"""S3-backed PDF archive (spec 0007).

`S3Archive` implements the Archive Protocol against Amazon S3. PDF
bytes are streamed from memory directly into `put_object`; no local
disk write ever occurs.

Security posture (see spec 0007 § Security):
  - SSE-S3 (AES256) on every PUT
  - No ACL parameter on PUT — objects inherit bucket-default private ACL
  - `source-url` metadata percent-encoded with `safe=''` as CRLF defense
  - Truncated URL never splits a `%XX` triplet
  - Startup `head_bucket` probe detects region / 404 / 403 fast
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)

from . import config


log = logging.getLogger(__name__)


_MAX_SOURCE_URL_LEN = config.MAX_S3_METADATA_URL_LEN
_ASCII_SAFE = re.compile(r"^[\x21-\x7e]*$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ArchiveSetupError(RuntimeError):
    """Startup misconfiguration — bucket missing, wrong region, 403, …"""


class ArchiveSizeError(RuntimeError):
    """PDF body exceeds the 50 MB ceiling at PUT time."""


def _truncate_respecting_percent_triplets(s: str, limit: int) -> str:
    """Truncate to `limit` chars, but never leave a dangling %XX tail."""
    if len(s) <= limit:
        return s
    cut = limit
    if cut >= 2 and s[cut - 2] == "%":
        cut -= 2
    elif cut >= 1 and s[cut - 1] == "%":
        cut -= 1
    return s[:cut]


def _encode_s3_metadata(raw: dict) -> dict:
    """Encode metadata values to satisfy S3's ASCII rules.

    `source-url` is percent-encoded with `safe=''` (strict) so that any
    CRLF or other HTTP-header-special chars land as literal `%0D%0A`,
    blocking header injection. Other keys (EIN, run-id, ISO timestamp)
    are already ASCII — pass through after an ASCII sanity check.

    Non-ASCII leftovers in any key are dropped with a structured warning
    rather than failing the whole PUT.
    """
    out: dict[str, str] = {}
    for k, v in raw.items():
        if v is None:
            continue
        if k == "source-url":
            encoded = quote(str(v), safe="")
            encoded = _truncate_respecting_percent_triplets(
                encoded, _MAX_SOURCE_URL_LEN
            )
            if not _ASCII_SAFE.match(encoded):
                log.warning("s3_metadata_encoding_failed key=%s", k)
                continue
            out[k] = encoded
        else:
            str_v = str(v)
            if not _ASCII_SAFE.match(str_v):
                log.warning("s3_metadata_encoding_failed key=%s", k)
                continue
            out[k] = str_v
    return out


class S3Archive:
    scheme = "s3"

    def __init__(
        self,
        bucket: str,
        prefix: str = config.DEFAULT_S3_PREFIX,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        client: Any = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = (prefix or "").strip("/")
        self._region = region
        # Spec 0007: LAVANDULA_S3_ENDPOINT_URL overrides the AWS default
        # endpoint (moto/minio/localstack testing). Kwarg wins over env.
        effective_endpoint = (
            endpoint_url
            if endpoint_url is not None
            else os.getenv("LAVANDULA_S3_ENDPOINT_URL")
        )
        self._endpoint_url = effective_endpoint
        self._client = client if client is not None else self._make_client(
            region, effective_endpoint
        )

    @staticmethod
    def _make_client(region: str | None, endpoint_url: str | None):
        # AC8: pin boto3's retry behavior so transient S3 5xx errors are
        # retried predictably (3 attempts total, exponential backoff via
        # the "standard" mode) rather than inheriting ambient defaults.
        cfg = Config(retries={"max_attempts": 3, "mode": "standard"})
        return boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            config=cfg,
        )

    def _key(self, sha256: str) -> str:
        if not _SHA256_RE.match(sha256):
            raise ValueError(f"invalid sha256: {sha256!r}")
        return (
            f"{self.prefix}/{sha256}.pdf" if self.prefix else f"{sha256}.pdf"
        )

    # ------------------------------------------------------------------
    # Archive Protocol
    # ------------------------------------------------------------------

    def put(self, sha256: str, body: bytes, metadata: dict) -> None:
        if len(body) > config.MAX_PDF_BYTES:
            raise ArchiveSizeError(
                f"PDF exceeds 50MB cap: {len(body)} bytes"
            )
        encoded_metadata = _encode_s3_metadata(metadata or {})
        kwargs = {
            "Bucket": self.bucket,
            "Key": self._key(sha256),
            "Body": body,
            "ContentLength": len(body),
            "ContentType": "application/pdf",
            "ServerSideEncryption": "AES256",
            "Metadata": encoded_metadata,
        }
        # Intentionally no ACL parameter (AC14) — inherit bucket default.
        self._client.put_object(**kwargs)

    def get(self, sha256: str) -> bytes:
        resp = self._client.get_object(
            Bucket=self.bucket, Key=self._key(sha256)
        )
        return resp["Body"].read()

    def head(self, sha256: str) -> dict | None:
        try:
            resp = self._client.head_object(
                Bucket=self.bucket, Key=self._key(sha256)
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return {
            "size": resp.get("ContentLength"),
            "metadata": resp.get("Metadata", {}),
        }

    def startup_probe(self) -> None:
        configured_region = getattr(self._client.meta, "region_name", None)

        # Round-3 review: do NOT hard-fail on missing configured_region.
        # IMDS-only deployments can yield credentials without populating
        # region_name; head_bucket's response header gives us the working
        # region as a fallback. We only fail with "no region configured"
        # if BOTH sources yield nothing.
        try:
            resp = self._client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            headers = (
                exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
            )
            actual = headers.get("x-amz-bucket-region")
            if code in ("404", "NoSuchBucket"):
                raise ArchiveSetupError(
                    f"bucket {self.bucket} does not exist"
                ) from exc
            if code in ("403", "AccessDenied"):
                raise ArchiveSetupError(
                    f"no permission to access bucket {self.bucket}"
                ) from exc
            if actual and configured_region and actual != configured_region:
                raise ArchiveSetupError(
                    f"bucket {self.bucket} is in region {actual}; "
                    f"client configured for {configured_region}"
                ) from exc
            if actual and not configured_region:
                # No client region, but the bucket told us where it is.
                # Soft success — log and return.
                log.info(
                    "s3_bucket_region_resolved_from_head bucket=%s region=%s",
                    self.bucket, actual,
                )
                return
            if not configured_region and not actual:
                raise ArchiveSetupError(
                    "no AWS region configured; pass --s3-region or "
                    "set AWS_REGION"
                ) from exc
            raise ArchiveSetupError(
                f"head_bucket failed: {code or exc}"
            ) from exc
        except (EndpointConnectionError, NoCredentialsError) as exc:
            raise ArchiveSetupError(
                f"could not connect to S3 or load credentials: {exc}"
            ) from exc

        headers = resp.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        actual_region = headers.get("x-amz-bucket-region")
        if actual_region and configured_region and \
                actual_region != configured_region:
            raise ArchiveSetupError(
                f"bucket {self.bucket} is in region {actual_region}; "
                f"client configured for {configured_region}"
            )
        if not configured_region and not actual_region:
            raise ArchiveSetupError(
                "no AWS region configured; pass --s3-region or "
                "set AWS_REGION"
            )

        # Defense-in-depth: warn if versioning is off. Missing permission
        # for GetBucketVersioning is not a failure — minimal runtime
        # policy does not grant it.
        try:
            ver = self._client.get_bucket_versioning(Bucket=self.bucket)
            if ver.get("Status") != "Enabled":
                log.warning(
                    "s3_bucket_versioning_disabled bucket=%s; "
                    "spec requires Enabled for durability",
                    self.bucket,
                )
        except ClientError:
            log.warning(
                "s3_bucket_versioning_check_skipped bucket=%s "
                "(missing s3:GetBucketVersioning permission)",
                self.bucket,
            )


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse `s3://bucket/prefix` into `(bucket, prefix)`.

    Trailing slashes on the prefix are stripped. A bare `s3://bucket`
    returns an empty prefix — the caller can substitute the default.
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3 URI: {uri!r}")
    rest = uri[len("s3://"):]
    if "/" in rest:
        bucket, prefix = rest.split("/", 1)
    else:
        bucket, prefix = rest, ""
    if not bucket:
        raise ValueError(f"empty bucket in s3 URI: {uri!r}")
    return bucket, prefix.strip("/")


__all__ = [
    "S3Archive",
    "ArchiveSetupError",
    "ArchiveSizeError",
    "parse_s3_uri",
]

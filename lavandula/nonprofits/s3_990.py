"""S3-backed archive for IRS 990 corpus (Spec 0030).

Manages batch zips and extracted per-org XML files in the
lavandula-990-corpus bucket. All keys are validated against strict
patterns to prevent path traversal or SSRF via crafted inputs.

Bucket structure:
    s3://lavandula-990-corpus/
      zips/{year}/{batch_id}.zip
      xml/{ein}/{object_id}.xml
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
from typing import IO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

_EIN_RE = re.compile(r"^\d{9}$", re.ASCII)
_OBJECT_ID_RE = re.compile(r"^\d+$", re.ASCII)
_BATCH_ID_RE = re.compile(r"^\d{4}_TEOS_XML_(0[1-9]|1[0-2])[A-D]$", re.ASCII)

_DEFAULT_BUCKET = "lavandula-990-corpus"
_MULTIPART_THRESHOLD = 100 * 1024 * 1024  # 100 MB
_SPILL_THRESHOLD = 500 * 1024 * 1024  # 500 MB


def _validate_ein(ein: str) -> None:
    if not _EIN_RE.match(ein):
        raise ValueError(f"Invalid EIN: {ein!r}")


def _validate_object_id(object_id: str) -> None:
    if not _OBJECT_ID_RE.match(object_id):
        raise ValueError(f"Invalid object_id: {object_id!r}")


def _validate_batch_id(batch_id: str) -> None:
    if not _BATCH_ID_RE.match(batch_id):
        raise ValueError(f"Invalid batch_id: {batch_id!r}")


def _validate_year(year: int) -> None:
    if not (2017 <= year <= 2099):
        raise ValueError(f"Invalid year: {year}")


class S3990Archive:
    """S3 client for the 990 corpus bucket."""

    def __init__(
        self,
        bucket: str = _DEFAULT_BUCKET,
        *,
        endpoint_url: str | None = None,
        client=None,
    ):
        self.bucket = bucket
        effective_endpoint = (
            endpoint_url
            if endpoint_url is not None
            else os.getenv("LAVANDULA_S3_ENDPOINT_URL")
        )
        self._client = client if client is not None else self._make_client(
            effective_endpoint
        )

    @staticmethod
    def _make_client(endpoint_url: str | None):
        cfg = Config(retries={"max_attempts": 3, "mode": "standard"})
        return boto3.client(
            "s3",
            region_name="us-east-1",
            endpoint_url=endpoint_url,
            config=cfg,
        )

    def _zip_key(self, year: int, batch_id: str) -> str:
        _validate_year(year)
        _validate_batch_id(batch_id)
        return f"zips/{year}/{batch_id}.zip"

    def _xml_key(self, ein: str, object_id: str) -> str:
        _validate_ein(ein)
        _validate_object_id(object_id)
        return f"xml/{ein}/{object_id}.xml"

    def zip_exists(self, year: int, batch_id: str) -> bool:
        """HEAD check for cached batch zip."""
        key = self._zip_key(year, batch_id)
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def upload_zip(
        self, year: int, batch_id: str, stream: IO[bytes]
    ) -> str:
        """Upload IRS batch zip to S3. Returns ChecksumSHA256."""
        key = self._zip_key(year, batch_id)

        data = stream.read()
        resp = self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ServerSideEncryption="AES256",
            ChecksumAlgorithm="SHA256",
        )

        checksum = resp.get("ChecksumSHA256")
        if not checksum:
            raise RuntimeError(
                f"S3 did not return ChecksumSHA256 for {key}"
            )
        return checksum

    def verify_zip_integrity(
        self, year: int, batch_id: str, expected_checksum: str
    ) -> bool:
        """Verify cached zip integrity via ChecksumSHA256 comparison."""
        key = self._zip_key(year, batch_id)
        try:
            resp = self._client.head_object(
                Bucket=self.bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
        except ClientError:
            return False

        actual = resp.get("ChecksumSHA256", "")
        return actual == expected_checksum

    def open_zip(self, year: int, batch_id: str) -> IO[bytes]:
        """Download zip from S3 into a file-like object.

        Small zips (<500 MB) stay in memory. Large ones spill to a temp file.
        """
        key = self._zip_key(year, batch_id)

        resp = self._client.head_object(Bucket=self.bucket, Key=key)
        size = resp.get("ContentLength", 0)

        get_resp = self._client.get_object(Bucket=self.bucket, Key=key)
        body = get_resp["Body"]

        if size <= _SPILL_THRESHOLD:
            buf = io.BytesIO()
            for chunk in body.iter_chunks(chunk_size=65536):
                buf.write(chunk)
            buf.seek(0)
            return buf
        else:
            tmp = tempfile.SpooledTemporaryFile(
                max_size=_SPILL_THRESHOLD, mode="w+b"
            )
            for chunk in body.iter_chunks(chunk_size=65536):
                tmp.write(chunk)
            tmp.seek(0)
            return tmp

    def upload_xml(self, ein: str, object_id: str, data: bytes) -> str:
        """Upload extracted XML to S3. Returns the s3 key."""
        key = self._xml_key(ein, object_id)
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType="application/xml",
            ServerSideEncryption="AES256",
        )
        return key

    def read_xml(self, s3_key: str) -> bytes:
        """Read XML from S3 by key."""
        resp = self._client.get_object(Bucket=self.bucket, Key=s3_key)
        return resp["Body"].read()

    def xml_exists(self, ein: str, object_id: str) -> bool:
        """HEAD check for extracted XML."""
        key = self._xml_key(ein, object_id)
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def get_zip_checksum(self, year: int, batch_id: str) -> str | None:
        """Get stored ChecksumSHA256 for a cached zip."""
        key = self._zip_key(year, batch_id)
        try:
            resp = self._client.head_object(
                Bucket=self.bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
            return resp.get("ChecksumSHA256")
        except ClientError:
            return None

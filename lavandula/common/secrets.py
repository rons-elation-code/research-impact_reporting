"""Secrets access via AWS SSM Parameter Store.

All Lavandula secrets live under `/cloud2.lavandulagroup.com/<name>` in
us-east-1 as SecureString parameters. Access uses the EC2 instance
profile (`cloud2_lavandulagroup`), which is scoped to the matching
path prefix with `kms:Decrypt` on the default SSM KMS key.

For tests and local dev (no AWS creds), set the env var
`LAVANDULA_SECRET_<UPPER_SNAKE>` to override a specific secret. E.g.
for `brave-api-key`: `LAVANDULA_SECRET_BRAVE_API_KEY=xxx`.

Never log secret values. Never print them. The accessor functions
return the raw string to the caller and nothing else.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any

_PARAM_PREFIX = "/cloud2.lavandulagroup.com/"
_REGION = "us-east-1"


class SecretUnavailable(RuntimeError):
    """Raised when a secret cannot be fetched from SSM or overrides."""


def _env_var_name(param_short_name: str) -> str:
    """Convert 'brave-api-key' → 'LAVANDULA_SECRET_BRAVE_API_KEY'."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", param_short_name).upper().strip("_")
    return f"LAVANDULA_SECRET_{safe}"


@lru_cache(maxsize=64)
def get_secret(
    short_name: str,
    *,
    region: str = _REGION,
    ssm_client: Any | None = None,
) -> str:
    """Fetch `<prefix><short_name>` from SSM (SecureString, decrypted).

    Per-process cached via lru_cache. Override via env var is checked
    FIRST (zero-AWS-call fast path for tests).

    Parameters
    ----------
    short_name: e.g. 'brave-api-key' — NO leading slash, NO prefix.
    region: AWS region override; defaults to us-east-1.
    ssm_client: optional pre-built boto3 SSM client (for dependency
        injection in tests).

    Raises
    ------
    SecretUnavailable if both the env var is unset AND the SSM call
    fails.
    """
    override = os.environ.get(_env_var_name(short_name))
    if override:
        return override

    try:
        client = ssm_client or _default_ssm_client(region=region)
    except Exception as exc:
        raise SecretUnavailable(
            f"cannot build SSM client for {short_name!r}: {type(exc).__name__}"
        ) from exc

    try:
        resp = client.get_parameter(
            Name=f"{_PARAM_PREFIX}{short_name}",
            WithDecryption=True,
        )
    except Exception as exc:
        raise SecretUnavailable(
            f"SSM GetParameter failed for {short_name!r}: "
            f"{type(exc).__name__}"
        ) from exc

    value = (resp.get("Parameter") or {}).get("Value")
    if not value:
        raise SecretUnavailable(f"empty value for {short_name!r}")
    return value


def _default_ssm_client(*, region: str) -> Any:
    """Lazy boto3 import so non-secret callers don't pay the import cost."""
    import boto3
    return boto3.client("ssm", region_name=region)


def get_brave_api_key() -> str:
    """Convenience accessor. Used by the website resolver."""
    return get_secret("brave-api-key")


def get_serpex_api_key() -> str:
    """Convenience accessor. Used by the Serpex search adapter."""
    return get_secret("serpex-api-key")


def clear_cache() -> None:
    """Invalidate the per-process cache (test helper)."""
    get_secret.cache_clear()


__all__ = [
    "SecretUnavailable",
    "get_secret",
    "get_brave_api_key",
    "get_serpex_api_key",
    "clear_cache",
]

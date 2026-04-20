"""Unit tests for lavandula.common.secrets."""
from __future__ import annotations

import os

import pytest


def setup_function(_):
    """Clear the lru_cache between tests."""
    from lavandula.common.secrets import clear_cache
    clear_cache()


def test_env_override_wins_over_ssm(monkeypatch):
    """If LAVANDULA_SECRET_* is set, SSM is never called."""
    from lavandula.common.secrets import get_secret

    monkeypatch.setenv("LAVANDULA_SECRET_MY_KEY", "env-override-value")

    called = []

    class FailingClient:
        def get_parameter(self, **kw):
            called.append(kw)
            raise AssertionError("SSM should not be called when env var is set")

    result = get_secret("my-key", ssm_client=FailingClient())
    assert result == "env-override-value"
    assert called == []


def test_env_var_name_conversion():
    """Verify short-name → env-var mapping."""
    from lavandula.common.secrets import _env_var_name
    assert _env_var_name("brave-api-key") == "LAVANDULA_SECRET_BRAVE_API_KEY"
    assert _env_var_name("some.weird/key_name") == "LAVANDULA_SECRET_SOME_WEIRD_KEY_NAME"


def test_ssm_path_prefix():
    """Secrets must live under /cloud2.lavandulagroup.com/."""
    from lavandula.common.secrets import get_secret

    captured = {}

    class MockClient:
        def get_parameter(self, *, Name, WithDecryption):
            captured["Name"] = Name
            captured["WithDecryption"] = WithDecryption
            return {"Parameter": {"Value": "mock-value"}}

    result = get_secret("test-param", ssm_client=MockClient())
    assert result == "mock-value"
    assert captured["Name"] == "/cloud2.lavandulagroup.com/test-param"
    assert captured["WithDecryption"] is True


def test_ssm_failure_raises_secret_unavailable():
    from lavandula.common.secrets import SecretUnavailable, get_secret

    class BrokenClient:
        def get_parameter(self, **kw):
            raise RuntimeError("simulated AccessDenied")

    with pytest.raises(SecretUnavailable) as exc_info:
        get_secret("test-param", ssm_client=BrokenClient())
    # Error message must not contain the secret value
    assert "AccessDenied" not in str(exc_info.value)  # sanitized
    # But should identify which secret
    assert "test-param" in str(exc_info.value)


def test_empty_value_raises():
    from lavandula.common.secrets import SecretUnavailable, get_secret

    class EmptyClient:
        def get_parameter(self, **kw):
            return {"Parameter": {"Value": ""}}

    with pytest.raises(SecretUnavailable):
        get_secret("empty-secret", ssm_client=EmptyClient())


def test_cached_within_process(monkeypatch):
    """Second call returns cached value; no second SSM call."""
    from lavandula.common.secrets import get_secret, clear_cache
    clear_cache()

    call_count = {"n": 0}

    class CountingClient:
        def get_parameter(self, **kw):
            call_count["n"] += 1
            return {"Parameter": {"Value": "cached-value"}}

    c = CountingClient()
    v1 = get_secret("cache-test", ssm_client=c)
    v2 = get_secret("cache-test", ssm_client=c)
    assert v1 == v2 == "cached-value"
    # lru_cache keys on positional args including ssm_client; to test
    # true per-name caching, we call with the same client twice. The
    # cache key includes the ssm_client object, so calls with the
    # SAME client should hit the cache.
    # If call_count > 1 here, cache isn't working as expected.
    assert call_count["n"] == 1


def test_get_brave_api_key_uses_correct_path(monkeypatch):
    """get_brave_api_key() → /cloud2.lavandulagroup.com/brave-api-key."""
    from lavandula.common.secrets import get_brave_api_key, clear_cache
    clear_cache()
    monkeypatch.setenv("LAVANDULA_SECRET_BRAVE_API_KEY", "test-brave-key")
    assert get_brave_api_key() == "test-brave-key"


# --- live integration smoke test (talks to real SSM) ---


def test_live_ssm_brave_key_readable():
    """Smoke test: the real SSM parameter is readable + non-empty.

    This runs against the actual AWS account the host is on. Skips if
    the env vars indicate we're in a test-only environment without
    AWS credentials.
    """
    from lavandula.common.secrets import get_brave_api_key, clear_cache
    clear_cache()
    # Make sure env override is unset so we hit SSM
    os.environ.pop("LAVANDULA_SECRET_BRAVE_API_KEY", None)
    try:
        value = get_brave_api_key()
    except Exception as exc:
        pytest.skip(f"SSM unreachable in this environment: {type(exc).__name__}")
    # Just assert non-empty; don't print or log the value.
    assert len(value) > 0
    # Make sure it's not still the placeholder
    assert "PLACEHOLDER" not in value

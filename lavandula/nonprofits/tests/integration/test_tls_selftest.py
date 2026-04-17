"""AC3: TLS self-test authoritative gate (plan Phase 1 hybrid design).

The local known-bad-cert endpoint is THE gate; remote is advisory.
Two scenarios exercised here:

  1. Happy path — the dynamically-generated expired cert trips an SSL
     error (verification is on).
  2. MITM-succeeded path — we patch requests.get to return a 200 for the
     local endpoint, simulating "verification is silently disabled". The
     self-test MUST raise TLSMisconfigured.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from lavandula.nonprofits import http_client


def test_local_expired_cert_passes_gate():
    """A real SSL error on the local endpoint = verification IS on."""
    # tls_self_test() returns None on success (cert error detected).
    http_client.tls_self_test(
        remote_url="https://localhost.invalid:1",  # remote is advisory
        budget_sec=5.0,
    )


def test_local_cert_accepted_halts():
    """If the local endpoint 'succeeds', verification is disabled → halt."""
    from types import SimpleNamespace
    def _mock_get(url, timeout=0, verify=True):
        # Simulate 'verification silently disabled somewhere'
        return SimpleNamespace(status_code=200)

    with patch.object(http_client.requests, "get", side_effect=_mock_get):
        with pytest.raises(http_client.TLSMisconfigured):
            http_client.tls_self_test(
                remote_url="https://localhost.invalid:1",
                budget_sec=5.0,
            )

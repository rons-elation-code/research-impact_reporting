"""AC14 — sandbox rlimits + network denial + empty env."""
from __future__ import annotations

import json
import os
import sys

import pytest


linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Sandbox requires Linux namespaces"
)


@linux_only
def test_ac14_sandbox_runs_hello_world():
    from lavandula.reports.sandbox.runner import self_test
    # If namespaces or seccomp are unavailable, self_test raises SandboxUnavailable.
    from lavandula.reports.sandbox.runner import SandboxUnavailable
    try:
        self_test()
    except SandboxUnavailable as exc:
        pytest.skip(f"Sandbox not available on this host: {exc}")


@linux_only
def test_ac14_sandbox_denies_network():
    """AC14 — the extractor subprocess has no network access."""
    from lavandula.reports.sandbox.runner import run_untrusted_python, SandboxUnavailable
    try:
        result = run_untrusted_python(
            "import socket\n"
            "try:\n"
            "    socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(('8.8.8.8', 53))\n"
            "    print('CONNECTED')\n"
            "except Exception as e:\n"
            "    print(f'BLOCKED: {type(e).__name__}')\n"
        )
    except SandboxUnavailable as exc:
        pytest.skip(f"Sandbox not available on this host: {exc}")
    # Connect must not succeed.
    assert "CONNECTED" not in result.stdout
    assert "BLOCKED" in result.stdout or result.returncode != 0


@linux_only
def test_ac14_sandbox_rlimit_memory():
    """800 MB RLIMIT_AS should prevent a 1 GB allocation from succeeding."""
    from lavandula.reports.sandbox.runner import run_untrusted_python, SandboxUnavailable
    try:
        result = run_untrusted_python(
            "try:\n"
            "    x = bytearray(1024 * 1024 * 1024)\n"
            "    print('OK')\n"
            "except MemoryError:\n"
            "    print('MEMERR')\n",
        )
    except SandboxUnavailable as exc:
        pytest.skip(f"Sandbox not available on this host: {exc}")
    assert "OK" not in result.stdout

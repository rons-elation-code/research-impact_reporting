from __future__ import annotations

import time
from types import SimpleNamespace


class _FakeQueue:
    def get_nowait(self):
        raise __import__("queue").Empty


class _FakeProcess:
    def __init__(self, *, alive_after_join: bool):
        self._alive = True
        self._alive_after_join = alive_after_join
        self.terminated = False
        self.killed = False

    def start(self):
        return None

    def join(self, _timeout=None):
        self._alive = self._alive_after_join if not self.terminated else False

    def is_alive(self):
        return self._alive

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False


class _FakeContext:
    def __init__(self, proc: _FakeProcess):
        self._proc = proc

    def Queue(self, maxsize=1):  # noqa: N802
        return _FakeQueue()

    def Process(self, target, args, daemon):  # noqa: N802
        return self._proc


def test_pdf_structure_timeout_does_not_leak(monkeypatch):
    from lavandula.reports import fetch_pdf

    proc = _FakeProcess(alive_after_join=True)
    monkeypatch.setattr(
        fetch_pdf.multiprocessing,
        "get_context",
        lambda method: _FakeContext(proc),
    )

    ok, note = fetch_pdf._validate_pdf_structure(b"%PDF-1.7\n")
    assert (ok, note) == (False, "pdf_structure_timeout")
    assert proc.terminated

    # Regression for the leaked SIGALRM behavior: unrelated sleep
    # after the call must not be interrupted by a delayed timeout.
    time.sleep(0.01)


def test_pdf_structure_worker_failure_returns_explicit_note(monkeypatch):
    from lavandula.reports import fetch_pdf

    proc = _FakeProcess(alive_after_join=False)
    monkeypatch.setattr(
        fetch_pdf.multiprocessing,
        "get_context",
        lambda method: _FakeContext(proc),
    )

    ok, note = fetch_pdf._validate_pdf_structure(b"%PDF-1.7\n")
    assert (ok, note) == (False, "pdf_structure_worker_failed")

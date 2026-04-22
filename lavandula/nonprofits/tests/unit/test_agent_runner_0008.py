"""Unit tests for AgentRunner + ClaudeCodeAgentRunner (Spec 0008)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lavandula.nonprofits.agent_runner import (
    AGENT_ALLOWED_TOOLS,
    AGENT_DISALLOWED_TOOLS,
    AGENT_MAX_OUTPUT_BYTES,
    AgentInvocation,
    ClaudeCodeAgentRunner,
    FakeAgentRunner,
    PROMPT_VERSION,
    build_claude_argv,
    resolve_spawn_prefix,
    render_prompt,
)


def _write_input(tmp_path: Path, orgs: list[dict]) -> Path:
    p = tmp_path / "batch-000-input.jsonl"
    with open(p, "w") as f:
        for o in orgs:
            f.write(json.dumps(o) + "\n")
    return p


def _inv(tmp_path: Path, orgs: list[dict], **overrides) -> AgentInvocation:
    input_path = _write_input(tmp_path, orgs)
    defaults = dict(
        batch_id=0,
        input_path=input_path,
        output_path=tmp_path / "batch-000-output.jsonl",
        model="haiku",
        timeout_sec=60,
        max_output_bytes=AGENT_MAX_OUTPUT_BYTES,
        tag_uuid="abcdef",
    )
    defaults.update(overrides)
    return AgentInvocation(**defaults)


# ── AC21: allowed/disallowed tools ─────────────────────────────────────────

def test_allowed_tools_constants_restrict_capabilities() -> None:
    assert set(AGENT_ALLOWED_TOOLS) == {"WebSearch", "WebFetch"}
    for forbidden in ("Bash", "Read", "Write", "Edit", "NotebookEdit"):
        assert forbidden in AGENT_DISALLOWED_TOOLS


def test_build_claude_argv_allow_list_mode(tmp_path: Path) -> None:
    inv = _inv(tmp_path, [{"ein": "1"*9, "name": "x"}])
    argv = build_claude_argv(inv, "PROMPT", mode="allow_list", prefix=[],
                             has_deny_list=True)
    # --allowed-tools present with WebSearch,WebFetch
    i = argv.index("--allowed-tools")
    assert argv[i + 1] == "WebSearch,WebFetch"
    # disallowed tools list includes Bash/Read/Write/Edit
    j = argv.index("--disallowed-tools")
    disallowed = argv[j + 1]
    for bad in ("Bash", "Read", "Write", "Edit"):
        assert bad in disallowed
    # model + prompt appear
    assert "--model" in argv
    assert "haiku" in argv
    assert "-p" in argv


def test_build_claude_argv_omits_disallowed_when_unsupported(tmp_path: Path) -> None:
    inv = _inv(tmp_path, [{"ein": "1"*9}])
    argv = build_claude_argv(inv, "PROMPT", mode="allow_list", prefix=[],
                             has_deny_list=False)
    assert "--allowed-tools" in argv
    # Must NOT include --disallowed-tools when CLI lacks the flag —
    # emitting an unrecognised flag would break every invocation.
    assert "--disallowed-tools" not in argv


def test_build_claude_argv_sandbox_mode(tmp_path: Path) -> None:
    inv = _inv(tmp_path, [{"ein": "1"*9}])
    argv = build_claude_argv(
        inv, "PROMPT", mode="sandbox",
        prefix=["firejail", "--quiet", "--private-tmp"],
    )
    # Sandbox prefix before claude invocation
    assert argv[0] == "firejail"
    assert "claude" in argv
    # In sandbox mode, we don't pass --allowed-tools (CLI doesn't support it)
    assert "--allowed-tools" not in argv


def test_sandbox_prefix_allows_network_access() -> None:
    """WebSearch+WebFetch need network. Sandbox must NOT cut it."""
    from lavandula.nonprofits.agent_runner import _detect_sandbox_prefix
    with patch("lavandula.nonprofits.agent_runner.shutil.which") as which:
        which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("firejail", "bwrap") else None
        prefix = _detect_sandbox_prefix()
    assert prefix is not None
    # No network-killing flags.
    joined = " ".join(prefix)
    assert "--net=none" not in joined
    assert "--unshare-net" not in joined


def test_resolve_spawn_prefix_prefers_allow_list() -> None:
    prefix, mode, deny = resolve_spawn_prefix(
        "Options:\n  --allowed-tools TOOLS\n"
    )
    assert mode == "allow_list"
    assert prefix == []
    assert deny is False


def test_resolve_spawn_prefix_detects_deny_list_capability() -> None:
    prefix, mode, deny = resolve_spawn_prefix(
        "Options:\n  --allowed-tools TOOLS\n  --disallowed-tools TOOLS\n"
    )
    assert mode == "allow_list"
    assert deny is True


def test_resolve_spawn_prefix_falls_back_to_sandbox() -> None:
    with patch("lavandula.nonprofits.agent_runner.shutil.which") as which:
        which.side_effect = lambda name: f"/usr/bin/{name}" if name == "firejail" else None
        prefix, mode, deny = resolve_spawn_prefix("no allow list flag here")
        assert mode == "sandbox"
        assert prefix[0] == "firejail"
        assert deny is False


def test_resolve_spawn_prefix_fails_without_both() -> None:
    with patch("lavandula.nonprofits.agent_runner.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="Security policy"):
            resolve_spawn_prefix("no allow list flag here")


# ── prompt ──────────────────────────────────────────────────────────────────

def test_render_prompt_wraps_input_in_untrusted_tags(tmp_path: Path) -> None:
    inv = _inv(tmp_path, [{"ein": "1" * 9, "name": "Acme"}], tag_uuid="TAGID")
    prompt = render_prompt(inv)
    assert "<untrusted_org_input_TAGID>" in prompt
    assert "</untrusted_org_input_TAGID>" in prompt
    assert "UNTRUSTED DATA" in prompt
    # The JSON line should appear inside the tags.
    assert '"ein": "111111111"' in prompt or '"ein":"111111111"' in prompt


# ── FakeAgentRunner ─────────────────────────────────────────────────────────

def test_fake_runner_produces_deterministic_output(tmp_path: Path) -> None:
    orgs = [{"ein": f"{i:09d}", "name": f"Org{i}"} for i in range(3)]
    inv = _inv(tmp_path, orgs)
    result = FakeAgentRunner().run(inv)
    assert result.state == "complete"
    assert result.completed_count == 3
    lines = inv.output_path.read_text().strip().splitlines()
    assert len(lines) == 3
    row = json.loads(lines[0])
    assert row["confidence"] == "high"
    assert row["url"].startswith("https://fake-")


# ── ClaudeCodeAgentRunner — mocked subprocess ──────────────────────────────

class _FakePopen:
    """Minimal Popen stand-in for timeout + size-cap tests."""

    def __init__(self, lines: list[bytes], *, hang: bool = False,
                 exit_code: int = 0):
        self._lines = list(lines)
        self._hang = hang
        self._exit_code = exit_code
        self.stdout = self
        self.stderr = self
        self.returncode: int | None = None
        self.pid = 99999
        self._killed = False
        self._emitted = 0

    def readline(self):
        if self._hang:
            # Simulate slow/empty pipe
            return b""
        if self._emitted < len(self._lines):
            self._emitted += 1
            return self._lines[self._emitted - 1]
        return b""

    def read(self):
        return b""

    def poll(self):
        if self._hang and not self._killed:
            return None
        if self._emitted >= len(self._lines) or self._killed:
            if self.returncode is None:
                self.returncode = self._exit_code
            return self.returncode
        return None

    def terminate(self):
        self._killed = True
        self.returncode = -15

    def kill(self):
        self._killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self._killed = True
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode


def test_claude_runner_timeout_terminates(tmp_path: Path, monkeypatch) -> None:
    inv = _inv(tmp_path, [{"ein": "1" * 9, "name": "x"}], timeout_sec=1)
    runner = ClaudeCodeAgentRunner(spawn_prefix=[], mode="allow_list")
    fp = _FakePopen([], hang=True)
    with patch("lavandula.nonprofits.agent_runner.subprocess.Popen",
               return_value=fp):
        # Speed up the poll loop.
        monkeypatch.setattr(
            "lavandula.nonprofits.agent_runner.time.monotonic",
            _counter(start=0, step=0.6),
        )
        monkeypatch.setattr(
            "lavandula.nonprofits.agent_runner.time.sleep",
            lambda s: None,
        )
        result = runner.run(inv)
    assert result.state == "timeout"
    assert fp._killed is True


def test_claude_runner_output_size_cap_terminates(tmp_path: Path) -> None:
    inv = _inv(tmp_path, [{"ein": "1" * 9}],
               max_output_bytes=100)
    runner = ClaudeCodeAgentRunner(spawn_prefix=[], mode="allow_list")
    big = b"x" * 200 + b"\n"
    fp = _FakePopen([big])
    with patch("lavandula.nonprofits.agent_runner.subprocess.Popen",
               return_value=fp):
        result = runner.run(inv)
    assert result.state == "failed"
    assert "output exceeded" in (result.error or "")


def test_claude_runner_output_size_cap_is_2mb() -> None:
    assert AGENT_MAX_OUTPUT_BYTES == 2 * 1024 * 1024


def test_claude_runner_cli_not_found(tmp_path: Path) -> None:
    inv = _inv(tmp_path, [{"ein": "1" * 9}])
    runner = ClaudeCodeAgentRunner(spawn_prefix=[], mode="allow_list")
    with patch("lavandula.nonprofits.agent_runner.subprocess.Popen",
               side_effect=FileNotFoundError):
        result = runner.run(inv)
    assert result.state == "failed"
    assert "claude CLI not found" in (result.error or "")


def test_claude_runner_clean_exit_complete(tmp_path: Path) -> None:
    orgs = [{"ein": f"{i:09d}"} for i in range(2)]
    inv = _inv(tmp_path, orgs)
    runner = ClaudeCodeAgentRunner(spawn_prefix=[], mode="allow_list")
    lines = [
        (json.dumps({"ein": "000000000", "url": "https://a.org",
                     "confidence": "high", "reasoning": "ok"}) + "\n").encode(),
        (json.dumps({"ein": "000000001", "url": "https://b.org",
                     "confidence": "high", "reasoning": "ok"}) + "\n").encode(),
    ]
    fp = _FakePopen(lines)
    with patch("lavandula.nonprofits.agent_runner.subprocess.Popen",
               return_value=fp):
        result = runner.run(inv)
    assert result.state == "complete"
    assert result.completed_count == 2


# ── helpers ─────────────────────────────────────────────────────────────────

def _counter(*, start: float, step: float):
    t = [start]

    def fn():
        t[0] += step
        return t[0]

    return fn

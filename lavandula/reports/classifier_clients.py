"""Pluggable classifier backends (TICK-003 on Spec 0004).

Exposes `select_classifier_client()` which reads the
`CLASSIFIER_CLIENT` env var and returns either:

- `anthropic.Anthropic()` (default; unset env var) — direct API
  billing against `ANTHROPIC_API_KEY`. Budget ledger is
  authoritative.

- `CodexSubscriptionClient()` (env var `codex`) — shells out to
  the `codex` CLI, which consumes the operator's ChatGPT Business
  subscription quota. Budget ledger still records reservations
  for accounting continuity but the cap branch is moot (quota is
  managed by OpenAI; CLI banner surfaces remaining headroom).

The Codex shim fakes the Anthropic SDK's response shape by
exposing:

- `.content`: list with one synthesized `tool_use` block whose
  `.input` matches the `record_classification` tool schema from
  `classify.py`.
- `.usage.input_tokens` / `.usage.output_tokens`: crude heuristic
  estimates (len_in_bytes / 4) sufficient for the budget-ledger
  settle call; NOT suitable for billing.

Nothing in `classify.py`, `budget.py`, or the classifier prompt
is altered by this module.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from .logging_utils import sanitize


class CodexShimError(RuntimeError):
    """Raised when the `codex` subprocess fails, times out, or
    returns output that can't be parsed into the tool schema.

    Callers using `classify_first_page(raise_on_error=False)`
    convert this into `classification=None` with the existing
    AC16.2 fallback path. Per-call retry is NOT attempted here —
    the `--retry-null-classifications` batch flow is the right
    layer for that.
    """


# --- response shape duck-types ------------------------------------------


class _ToolUseBlock:
    """Fake Anthropic SDK content block of type='tool_use'.

    `classify.py::_parse_tool_use` reads `.type` and `.input`.
    """

    __slots__ = ("type", "name", "input")

    def __init__(self, name: str, inp: dict[str, Any]) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = inp


class _Usage:
    __slots__ = ("input_tokens", "output_tokens")

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = int(input_tokens)
        self.output_tokens = int(output_tokens)


class _Response:
    """Duck-type of Anthropic SDK's messages.create response."""

    __slots__ = ("content", "usage", "stop_reason")

    def __init__(self, tool_use: _ToolUseBlock, usage: _Usage) -> None:
        self.content = [tool_use]
        self.usage = usage
        self.stop_reason = "tool_use"


# --- Codex shim ---------------------------------------------------------


# Allow dependency injection of the subprocess runner so tests can stub
# it without actually invoking `codex`.
_Runner = Any  # callable matching subprocess.run signature, roughly


def _minimal_env() -> dict[str, str]:
    """Return a minimal env dict for the `codex` subprocess.

    Forwarded vars: HOME and PATH only. This intentionally does NOT
    forward ANTHROPIC_API_KEY, OPENAI_API_KEY, AWS_*, or other
    secrets — they are not needed by `codex` for authentication
    (it uses its own OAuth token in ~/.codex/) and leaking them
    into the subprocess image would be an unnecessary exposure.
    """
    env = {}
    for var in ("HOME", "PATH"):
        val = os.environ.get(var)
        if val is not None:
            env[var] = val
    return env


def _strip_code_fences(text: str) -> str:
    """Strip leading/trailing ``` or ```json fences if the model
    wrapped its JSON in a code block despite the prompt.

    Defensive — Codex has a markdown reflex and occasionally adds
    fences even when told not to.
    """
    text = text.strip()
    fence_pattern = re.compile(r"^```(?:json|JSON)?\s*\n(.+)\n```\s*$", re.DOTALL)
    m = fence_pattern.match(text)
    if m:
        return m.group(1).strip()
    return text


def _estimate_tokens(text: str) -> int:
    """Crude token estimate: roughly 4 chars per token for English text.

    Used only for budget-ledger settlement values. Not billing-grade.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


class _Messages:
    """Fake of `anthropic.Anthropic().messages` with .create()."""

    def __init__(self, parent: "CodexSubscriptionClient") -> None:
        self._parent = parent

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> _Response:
        """Re-shape Anthropic-style kwargs into a Codex prompt, run
        the subprocess, parse the JSON reply, return a fake
        Anthropic Response object.
        """
        if not tools:
            raise CodexShimError("tools list is empty")
        tool = tools[0]
        schema = tool.get("input_schema") or {}
        tool_name = tool.get("name") or "record_classification"

        user_content = messages[-1]["content"] if messages else ""
        prompt = self._parent._build_prompt(
            system=system, user=user_content, schema=schema
        )
        raw_output = self._parent._invoke_codex(prompt)
        payload = self._parent._parse_json(raw_output)

        # Token estimate (crude): prompt in, raw reply out.
        usage = _Usage(
            input_tokens=_estimate_tokens(prompt),
            output_tokens=_estimate_tokens(raw_output),
        )
        block = _ToolUseBlock(name=tool_name, inp=payload)
        return _Response(block, usage)


class CodexSubscriptionClient:
    """Duck-type of `anthropic.Anthropic()` for classifier use.

    Parameters
    ----------
    timeout_sec:
        Max subprocess runtime before TimeoutExpired. Default 60s.
        The classifier prompt is small (first-page text capped at
        4096 chars) so 60s is generous.
    cli:
        Command name for the Codex CLI. Default 'codex'.
        Override for testing.
    runner:
        Optional callable with the `subprocess.run` signature. If
        provided, used instead of `subprocess.run` — enables
        deterministic unit tests without shelling out.
    """

    def __init__(
        self,
        *,
        timeout_sec: float = 60.0,
        cli: str = "codex",
        runner: _Runner | None = None,
        codex_model: str | None = None,
    ) -> None:
        self._timeout = timeout_sec
        self._cli = cli
        self._runner = runner or subprocess.run
        # Pull codex model from env var or explicit arg; None means
        # "use codex CLI's configured default" (currently gpt-5.4).
        self._codex_model = (
            codex_model
            or os.environ.get("CLASSIFIER_CODEX_MODEL")
            or None
        )
        self.messages = _Messages(self)

    # --- prompt shaping --------------------------------------------------

    def _build_prompt(self, *, system: str, user: str, schema: dict[str, Any]) -> str:
        """Fuse system + user into a single prompt asking for strict JSON."""
        schema_str = json.dumps(schema, indent=2)
        return (
            f"{system}\n\n"
            f"{user}\n\n"
            "Respond with ONLY a valid JSON object matching this schema.\n"
            "Do not include prose, markdown, code fences, or any\n"
            "explanation — emit just the JSON object.\n\n"
            f"Schema:\n{schema_str}\n"
        )

    # --- subprocess -----------------------------------------------------

    def _invoke_codex(self, prompt: str) -> str:
        """Shell out to `codex exec` with prompt on stdin, return stdout.

        Uses `codex exec -` so the prompt is piped via stdin (avoids
        argv size limits on long prompts). This is the non-interactive
        subcommand per `codex --help`.

        If `codex_model` is set (via constructor arg or
        CLASSIFIER_CODEX_MODEL env var), forwards it as
        `-c model=<name>` per the codex CLI's config-override flag.
        """
        cmd = [self._cli]
        if self._codex_model:
            cmd += ["-c", f"model={self._codex_model}"]
        cmd += ["exec", "-"]
        try:
            result = self._runner(
                cmd,
                input=prompt,
                capture_output=True,
                timeout=self._timeout,
                text=True,
                check=False,
                env=_minimal_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexShimError(
                f"codex CLI timed out after {self._timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise CodexShimError(
                f"codex CLI not found on PATH: {sanitize(str(exc))}"
            ) from exc

        returncode = getattr(result, "returncode", 0)
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        if returncode != 0:
            raise CodexShimError(
                f"codex CLI returned {returncode}: "
                f"stderr={sanitize(stderr)[:200]}"
            )
        if not stdout.strip():
            raise CodexShimError("codex CLI returned empty stdout")
        return stdout

    # --- parsing --------------------------------------------------------

    def _parse_json(self, raw: str) -> dict[str, Any]:
        """Parse Codex stdout as JSON; tolerate code fences."""
        cleaned = _strip_code_fences(raw)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise CodexShimError(
                f"codex reply was not valid JSON: "
                f"head={cleaned[:100]!r}"
            ) from exc
        if not isinstance(data, dict):
            raise CodexShimError(
                f"codex reply JSON was not an object: type={type(data).__name__}"
            )
        return data


# --- factory ------------------------------------------------------------


def select_classifier_client(
    *,
    env: dict[str, str] | None = None,
) -> Any:
    """Return the classifier client selected by `CLASSIFIER_CLIENT` env var.

    - Unset / empty / "anthropic": `anthropic.Anthropic()` (default).
    - "codex": `CodexSubscriptionClient()`.
    - Anything else: ValueError.

    Parameters
    ----------
    env:
        Optional override for testing. Defaults to `os.environ`.
    """
    env = env if env is not None else dict(os.environ)
    backend = (env.get("CLASSIFIER_CLIENT") or "").strip().lower()
    if backend in ("", "anthropic"):
        import anthropic  # imported lazily so Codex-only operators
        # don't need the anthropic package installed.
        return anthropic.Anthropic()
    if backend == "codex":
        return CodexSubscriptionClient()
    raise ValueError(
        f"unknown CLASSIFIER_CLIENT={backend!r}; "
        "expected '' | 'anthropic' | 'codex'"
    )


__all__ = [
    "CodexShimError",
    "CodexSubscriptionClient",
    "select_classifier_client",
]

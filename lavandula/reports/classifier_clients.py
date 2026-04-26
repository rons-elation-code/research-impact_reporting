"""Pluggable classifier backends via subscription CLIs.

Exposes `select_classifier_client()` which reads env vars and returns
a duck-typed client matching the Anthropic SDK's `messages.create()`
interface.

All backends shell out to subscription CLIs (codex, claude, gemini)
— never API keys. The client fakes the Anthropic SDK response shape:

- `.content`: list with one synthesized `tool_use` block whose
  `.input` matches the `record_classification` tool schema.
- `.usage.input_tokens` / `.usage.output_tokens`: crude estimates
  sufficient for the budget-ledger settle call.

Backend selection:
- `CLASSIFIER_CLIENT` env var: "gemini" (default) | "claude" | "codex"
- `CLASSIFIER_CLI_MODEL` env var: override the CLI's default model
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from .logging_utils import sanitize


class ClassifierCLIError(RuntimeError):
    """Raised when a subscription CLI subprocess fails, times out,
    or returns output that can't be parsed into the tool schema."""


# Keep old name as alias — existing code imports this
CodexShimError = ClassifierCLIError


# --- response shape duck-types ------------------------------------------


class _ToolUseBlock:
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
    __slots__ = ("content", "usage", "stop_reason")

    def __init__(self, tool_use: _ToolUseBlock, usage: _Usage) -> None:
        self.content = [tool_use]
        self.usage = usage
        self.stop_reason = "tool_use"


# --- helpers ------------------------------------------------------------


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    fence_pattern = re.compile(r"^```(?:json|JSON)?\s*\n(.+)\n```\s*$", re.DOTALL)
    m = fence_pattern.match(text)
    if m:
        return m.group(1).strip()
    return text


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _minimal_env() -> dict[str, str]:
    env = {}
    for var in ("HOME", "PATH"):
        val = os.environ.get(var)
        if val is not None:
            env[var] = val
    return env


# --- CLI backend configs ------------------------------------------------


_CLI_CONFIGS: dict[str, dict[str, Any]] = {
    "codex": {
        "cli": "codex",
        "prompt_flag": None,  # uses exec - (stdin)
        "model_flag": lambda m: ["-c", f"model={m}"],
        "json_flag": [],
        "parse_response": lambda raw: raw,  # raw stdout IS the response
    },
    "claude": {
        "cli": "claude",
        "prompt_flag": "-p",
        "model_flag": lambda m: ["--model", m],
        "json_flag": ["--output-format", "json"],
        "parse_response": lambda raw: json.loads(raw).get("result", raw),
    },
    "gemini": {
        "cli": "gemini",
        "prompt_flag": "-p",
        "model_flag": lambda m: ["-m", m],
        "json_flag": ["-o", "json"],
        "parse_response": lambda raw: json.loads(raw).get("response", raw),
    },
}


# --- Generic subscription CLI client -----------------------------------


class _Messages:
    def __init__(self, parent: "SubscriptionCLIClient") -> None:
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
        if not tools:
            raise ClassifierCLIError("tools list is empty")
        tool = tools[0]
        schema = tool.get("input_schema") or {}
        tool_name = tool.get("name") or "record_classification"

        user_content = messages[-1]["content"] if messages else ""
        prompt = self._parent._build_prompt(
            system=system, user=user_content, schema=schema
        )
        raw_output = self._parent._invoke_cli(prompt)
        payload = self._parent._parse_json(raw_output)

        usage = _Usage(
            input_tokens=_estimate_tokens(prompt),
            output_tokens=_estimate_tokens(raw_output),
        )
        block = _ToolUseBlock(name=tool_name, inp=payload)
        return _Response(block, usage)


class SubscriptionCLIClient:
    """Generic subscription CLI classifier client.

    Works with codex, claude, and gemini CLIs via a config-driven
    approach. All use subscription-based access — no API keys.
    """

    def __init__(
        self,
        *,
        backend: str = "gemini",
        timeout_sec: float = 60.0,
        cli_model: str | None = None,
        runner: Any | None = None,
    ) -> None:
        if backend not in _CLI_CONFIGS:
            raise ValueError(
                f"unknown backend {backend!r}; "
                f"expected one of {sorted(_CLI_CONFIGS)}"
            )
        self._backend = backend
        self._config = _CLI_CONFIGS[backend]
        self._timeout = timeout_sec
        self._cli = self._config["cli"]
        self._runner = runner or subprocess.run
        self._cli_model = (
            cli_model
            or os.environ.get("CLASSIFIER_CLI_MODEL")
            or None
        )
        self.messages = _Messages(self)

    def _build_prompt(self, *, system: str, user: str, schema: dict[str, Any]) -> str:
        schema_str = json.dumps(schema, indent=2)
        return (
            f"{system}\n\n"
            f"{user}\n\n"
            "Respond with ONLY a valid JSON object matching this schema.\n"
            "Do not include prose, markdown, code fences, or any\n"
            "explanation — emit just the JSON object.\n\n"
            f"Schema:\n{schema_str}\n"
        )

    def _invoke_cli(self, prompt: str) -> str:
        cmd = [self._cli]

        if self._cli_model:
            model_flag_fn = self._config["model_flag"]
            cmd += model_flag_fn(self._cli_model)

        prompt_flag = self._config["prompt_flag"]
        if prompt_flag is None:
            # codex-style: exec - (stdin pipe)
            cmd += ["exec", "-"]
            use_stdin = True
        else:
            cmd += [prompt_flag, prompt]
            use_stdin = False

        cmd += self._config["json_flag"]

        try:
            result = self._runner(
                cmd,
                input=prompt if use_stdin else None,
                capture_output=True,
                timeout=self._timeout,
                text=True,
                check=False,
                env=_minimal_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ClassifierCLIError(
                f"{self._cli} CLI timed out after {self._timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise ClassifierCLIError(
                f"{self._cli} CLI not found on PATH: {sanitize(str(exc))}"
            ) from exc

        returncode = getattr(result, "returncode", 0)
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        if returncode != 0:
            raise ClassifierCLIError(
                f"{self._cli} CLI returned {returncode}: "
                f"stderr={sanitize(stderr)[:200]}"
            )
        if not stdout.strip():
            raise ClassifierCLIError(f"{self._cli} CLI returned empty stdout")

        parse_fn = self._config["parse_response"]
        return parse_fn(stdout)

    def _parse_json(self, raw: str) -> dict[str, Any]:
        cleaned = _strip_code_fences(raw)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ClassifierCLIError(
                f"{self._cli} reply was not valid JSON: "
                f"head={cleaned[:100]!r}"
            ) from exc
        if not isinstance(data, dict):
            raise ClassifierCLIError(
                f"{self._cli} reply JSON was not an object: "
                f"type={type(data).__name__}"
            )
        return data


# Backwards compat alias
CodexSubscriptionClient = SubscriptionCLIClient


# --- DeepSeek API client ------------------------------------------------


_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
_DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"


class _DeepSeekMessages:
    def __init__(self, parent: "DeepSeekAPIClient") -> None:
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
        if not tools:
            raise ClassifierCLIError("tools list is empty")
        tool = tools[0]
        schema = tool.get("input_schema") or {}
        tool_name = tool.get("name") or "record_classification"

        user_content = messages[-1]["content"] if messages else ""
        schema_str = json.dumps(schema, indent=2)
        prompt = (
            f"{system}\n\n"
            f"{user_content}\n\n"
            "Respond with ONLY a valid JSON object matching this schema.\n"
            "Do not include prose, markdown, code fences, or any\n"
            "explanation — emit just the JSON object.\n\n"
            f"Schema:\n{schema_str}\n"
        )

        raw_output = self._parent._call_api(prompt, max_tokens=max_tokens)
        cleaned = _strip_code_fences(raw_output)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ClassifierCLIError(
                f"deepseek reply was not valid JSON: head={cleaned[:100]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise ClassifierCLIError(
                f"deepseek reply was not a JSON object: type={type(payload).__name__}"
            )

        usage = _Usage(
            input_tokens=_estimate_tokens(prompt),
            output_tokens=_estimate_tokens(raw_output),
        )
        block = _ToolUseBlock(name=tool_name, inp=payload)
        return _Response(block, usage)


class DeepSeekAPIClient:
    """DeepSeek API classifier client (OpenAI-compatible endpoint).

    Key is fetched from SSM at `deepseek-api-key` via the standard
    lavandula secrets module. Model defaults to deepseek-v4-flash.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout_sec: float = 30.0,
    ) -> None:
        from lavandula.common.secrets import get_secret
        self._api_key = get_secret("lavandula/deepseek/api_key")
        self._model = (
            model
            or os.environ.get("CLASSIFIER_CLI_MODEL")
            or _DEEPSEEK_DEFAULT_MODEL
        )
        self._timeout = timeout_sec
        self._backend = "deepseek"
        self._cli_model = self._model
        self.messages = _DeepSeekMessages(self)

    def _call_api(self, prompt: str, *, max_tokens: int = 1024) -> str:
        import requests
        resp = requests.post(
            f"{_DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.0,
            },
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise ClassifierCLIError(
                f"deepseek API returned {resp.status_code}: "
                f"{sanitize(resp.text[:200])}"
            )
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise ClassifierCLIError("deepseek API returned no choices")
        return choices[0].get("message", {}).get("content", "")


# --- factory ------------------------------------------------------------


_ALL_BACKENDS = sorted(list(_CLI_CONFIGS) + ["deepseek"])


def select_classifier_client(
    *,
    env: dict[str, str] | None = None,
) -> Any:
    """Return the classifier client selected by `CLASSIFIER_CLIENT` env var.

    Subscription CLIs: "gemini" (default) | "claude" | "codex"
    API clients:       "deepseek" (key from SSM)
    """
    env = env if env is not None else dict(os.environ)
    backend = (env.get("CLASSIFIER_CLIENT") or "gemini").strip().lower()
    if backend == "deepseek":
        return DeepSeekAPIClient()
    if backend in _CLI_CONFIGS:
        return SubscriptionCLIClient(backend=backend)
    raise ValueError(
        f"unknown CLASSIFIER_CLIENT={backend!r}; "
        f"expected one of {_ALL_BACKENDS}"
    )


__all__ = [
    "ClassifierCLIError",
    "CodexShimError",
    "CodexSubscriptionClient",
    "DeepSeekAPIClient",
    "SubscriptionCLIClient",
    "select_classifier_client",
]

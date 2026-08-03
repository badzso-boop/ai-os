"""Anthropic MCP adapter (doc 07/15) — two auth modes, one adapter.

Implements `BaseMCPAdapter` for Anthropic's Claude models. Two independent
execution paths, selected by constructor flags:

1. **CLI session mode** (`use_cli_session=True`) — shells out to the
   official, already-authenticated `claude` CLI (`claude -p ... --output-format
   json`) in non-interactive print mode. This consumes the user's Claude
   subscription's included usage through Anthropic's own sanctioned
   scripting interface, as opposed to scraping the `claude.ai` browser
   session cookie and calling its private internal API — see
   `docs/15_PROVIDER_AUTHENTICATION_AND_ROUTING.md`'s "Native Web Session"
   idea, which is explicitly a reverse-engineering/ToS-risk approach this
   adapter does NOT take.
2. **API-key mode** (`api_key=...`) — a normal `httpx.AsyncClient` POST to
   `https://api.anthropic.com/v1/messages`, i.e. the standard pay-per-token
   Messages API, for users who want metered billing instead of subscription
   usage.

Design notes for the CLI-session path (see the task brief for the verified
ground-truth JSON shape this was built against):

- **Tool access is fully locked down.** This adapter is a pure "prompt in,
  text out" completion call — AI-OS's own Git worktree/patch-application
  layer (Phase 2, `ai_os.core.staging`) is what mediates real file changes,
  not a raw Claude Code session spawned by this adapter. Every CLI
  invocation passes `--permission-mode plan` (read-only exploration; blocks
  all edits) *and* `--disallowedTools` covering every mutating/exploratory
  tool, so the subprocess can't touch the filesystem or the network on its
  own even if a prompt tried to talk it into it.
- **No project-context auto-discovery.** The `claude` CLI auto-loads
  CLAUDE.md/project memory from its working directory even with tools
  disallowed (confirmed empirically: ~15K cache-creation tokens on a
  trivial "pong" prompt) — pure waste for a stateless completion call. The
  documented `--bare` flag suppresses this, but `--bare` *also* forces
  API-key-only auth per its own `--help` text ("OAuth and keychain are
  never read"), which would silently defeat the whole point of session
  mode. Instead, we run the subprocess with `cwd` set to a scratch
  directory that is lazily created once per adapter instance and contains
  no CLAUDE.md/`.git`/project files for the CLI to discover — see
  `_scratch_dir()`. Reused across calls (not per-call) because there is
  nothing call-specific in it and recreating an empty directory per call
  buys nothing but extra filesystem churn.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import List

import httpx

from ai_os.mcp.adapters.base_adapter import (
    BaseMCPAdapter,
    LLMTaskRequest,
    LLMTaskResponse,
    TokenUsage,
)

DEFAULT_MODEL = "claude-sonnet-4-5"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

# Locks the CLI-session subprocess down to a pure prompt-in/text-out call —
# see module docstring point 1. Matches the verified smoke-test invocation
# exactly; kept as a module-level constant so it's obviously one shared list
# rather than something that could drift between call sites.
DISALLOWED_TOOLS = "Bash Edit Write NotebookEdit Read Glob Grep WebFetch WebSearch"


class AnthropicCliError(RuntimeError):
    """The `claude` CLI subprocess failed, or reported `is_error: true`.

    Raised both for a non-zero process exit and for a zero-exit process
    whose parsed JSON payload says `is_error: true` — the CLI can report a
    task-level failure (e.g. the model errored internally) while still
    exiting 0, so both signals have to be checked.
    """

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"claude CLI failed (exit {returncode}): {stderr.strip()}"
        )


class AnthropicCliOutputError(RuntimeError):
    """The `claude` CLI's stdout wasn't the single JSON object we expected.

    Distinguished from `AnthropicCliError` because this is a "the CLI's
    output contract changed on us" failure (unparseable stdout), not a
    "the CLI told us it failed" outcome — raising this instead of letting
    `json.JSONDecodeError` bubble up gives callers a clear, on-topic
    exception instead of an opaque parser traceback with no context about
    which subprocess produced the bad output.
    """

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        super().__init__(
            f"claude CLI did not produce parseable JSON on stdout: {stdout[:500]!r}"
        )


class AnthropicApiError(RuntimeError):
    """The Anthropic Messages API returned a non-2xx response."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Anthropic API request failed (HTTP {status_code}): {body[:500]}"
        )


class AnthropicAdapter(BaseMCPAdapter):
    """Talks to Anthropic's Claude models, via the CLI session or the API.

    Exactly one of `use_cli_session` / `api_key` should be configured;
    `execute_task` raises `ValueError` if neither is. Both fields can be set
    on the same instance (e.g. to keep a fallback path available), in which
    case `use_cli_session` takes priority — see `execute_task`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        use_cli_session: bool = False,
        model: str = DEFAULT_MODEL,
        claude_cli: str = "claude",
    ) -> None:
        self.api_key = api_key
        self.use_cli_session = use_cli_session
        self.model = model
        self.claude_cli = claude_cli
        # Lazily created on first CLI-session call, then reused for the
        # lifetime of this adapter instance — see module docstring. `None`
        # until `_scratch_dir()` is first called.
        self._scratch_dir_path: Path | None = None

    def _scratch_dir(self) -> Path:
        """Return the adapter's scratch cwd for CLI invocations, creating it
        on first use. Reused (not recreated) across calls: it's just an
        empty directory with nothing call-specific in it, so there is
        nothing to gain from a fresh one every time and doing so would just
        add filesystem churn per `execute_task` call.
        """
        if self._scratch_dir_path is None or not self._scratch_dir_path.exists():
            self._scratch_dir_path = Path(
                tempfile.mkdtemp(prefix="ai-os-anthropic-")
            )
        return self._scratch_dir_path

    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:
        if self.use_cli_session:
            return await self._execute_via_cli(request)
        if self.api_key:
            return await self._execute_via_api(request)
        raise ValueError(
            "AnthropicAdapter requires either use_cli_session=True or an api_key"
        )

    # -- CLI session mode ---------------------------------------------------

    async def _execute_via_cli(self, request: LLMTaskRequest) -> LLMTaskResponse:
        model = request.model or self.model
        argv: List[str] = [
            self.claude_cli,
            "-p",
            request.context_payload,
            "--output-format",
            "json",
            "--model",
            model,
            "--permission-mode",
            "plan",
            "--disallowedTools",
            DISALLOWED_TOOLS,
        ]
        if request.system_prompt:
            argv += ["--system-prompt", request.system_prompt]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self._scratch_dir()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode is not None
        stdout_text = stdout.decode()
        stderr_text = stderr.decode()

        if proc.returncode != 0:
            raise AnthropicCliError(proc.returncode, stderr_text)

        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise AnthropicCliOutputError(stdout_text) from exc

        if payload.get("is_error"):
            # Zero exit but the CLI itself flagged failure — stderr may be
            # empty in this case, so fall back to the JSON payload for a
            # useful message.
            raise AnthropicCliError(0, stderr_text or json.dumps(payload))

        usage = payload.get("usage") or {}
        return LLMTaskResponse(
            task_id=request.task_id,
            provider="anthropic",
            model_name=model,
            generated_text=payload.get("result", ""),
            usage=TokenUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                estimated_usd_cost=payload.get("total_cost_usd", 0.0),
            ),
        )

    # -- API-key mode --------------------------------------------------------

    async def _execute_via_api(self, request: LLMTaskRequest) -> LLMTaskResponse:
        model = request.model or self.model
        headers = {
            "x-api-key": self.api_key or "",
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": 4096,
            "system": request.system_prompt,
            "messages": [{"role": "user", "content": request.context_payload}],
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(ANTHROPIC_API_URL, headers=headers, json=body)

        if response.status_code // 100 != 2:
            raise AnthropicApiError(response.status_code, response.text)

        payload = response.json()
        content = payload.get("content") or []
        text = content[0]["text"] if content else ""
        usage = payload.get("usage") or {}

        return LLMTaskResponse(
            task_id=request.task_id,
            provider="anthropic",
            model_name=model,
            generated_text=text,
            usage=TokenUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                # The Messages API doesn't return a cost estimate directly;
                # left at the TokenUsage default (0.0) rather than guessed
                # at from a hardcoded per-token price table that would go
                # stale the moment Anthropic repriced a model.
            ),
        )

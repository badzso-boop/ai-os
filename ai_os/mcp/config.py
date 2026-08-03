"""Environment-based construction of MCP provider adapters (doc 15).

An adapter is only built for a provider when real evidence of credentials
exists (an env var actually set, or the `claude` binary actually found on
`PATH`) — never assumed. Providers with no evidence are silently omitted
from the returned dict; callers (the router, the CLI) decide what to do
about a missing provider rather than this module raising on their behalf.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

from ai_os.mcp.adapters.anthropic_adapter import AnthropicAdapter
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter
from ai_os.mcp.adapters.gemini_adapter import GeminiAdapter
from ai_os.mcp.adapters.openrouter_adapter import OpenRouterAdapter


def load_configured_adapters(
    env_file: str | Path | None = None, environ: dict[str, str] | None = None
) -> dict[str, BaseMCPAdapter]:
    """Build every adapter whose credentials are actually present.

    `env_file` is passed straight to `load_dotenv` (None -> its own default
    search behavior, e.g. a `.env` in the current directory); already-set
    real environment variables are never overridden by the `.env` file.
    `environ` lets tests inject a fake environment instead of the real
    `os.environ` / a real `.env` file.
    """
    if environ is None:
        load_dotenv(dotenv_path=env_file, override=False)
        environ = os.environ  # type: ignore[assignment]

    adapters: dict[str, BaseMCPAdapter] = {}

    claude_cli_name = environ.get("ANTHROPIC_CLI", "claude")
    # Pass `path=` explicitly (rather than relying on shutil.which's own
    # os.environ["PATH"] default) so a test-injected `environ` dict is fully
    # authoritative — when `environ` really is os.environ, this is exactly
    # the real PATH anyway, so production behavior is unchanged.
    # `shutil.which(..., path=None)` falls back to the *real* os.environ["PATH"]
    # even when a fake `environ` dict was injected — pass "" (search nothing)
    # rather than None when the injected environ has no PATH key, so a test's
    # empty environ can't accidentally see this machine's real `claude` binary.
    claude_cli_path = shutil.which(claude_cli_name, path=environ.get("PATH", ""))
    anthropic_api_key = environ.get("ANTHROPIC_API_KEY")
    anthropic_mode = environ.get("ANTHROPIC_MODE")
    if anthropic_mode is None:
        anthropic_mode = "session" if claude_cli_path else ("api_key" if anthropic_api_key else None)

    if anthropic_mode == "session" and claude_cli_path:
        adapters["anthropic"] = AnthropicAdapter(use_cli_session=True, claude_cli=claude_cli_path)
    elif anthropic_mode == "api_key" and anthropic_api_key:
        adapters["anthropic"] = AnthropicAdapter(api_key=anthropic_api_key)

    gemini_api_key = environ.get("GEMINI_API_KEY")
    if gemini_api_key:
        adapters["gemini"] = GeminiAdapter(api_key=gemini_api_key)

    openrouter_api_key = environ.get("OPENROUTER_API_KEY")
    if openrouter_api_key:
        adapters["openrouter"] = OpenRouterAdapter(
            api_key=openrouter_api_key,
            model=environ.get("OPENROUTER_DEFAULT_MODEL") or None,
            site_url=environ.get("OPENROUTER_SITE_URL") or None,
            app_title=environ.get("OPENROUTER_APP_TITLE") or None,
        )

    return adapters

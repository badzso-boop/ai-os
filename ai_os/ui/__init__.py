"""UI-debug toolchain — DETERMINISTIC core (see docs/19_UI_DEBUG_TOOLCHAIN.md).

This package holds the zero-LLM, zero-network layer that turns a project's
HTML/CSS/JS into a queryable UI Knowledge Graph and flags likely UI-bug patterns,
so an (LLM) agent can be handed a compressed, suspicion-ranked context instead of
a raw code dump. The dynamic Playwright probe, the LLM triage/routing, and the
CLI command are deliberately OUT of scope for the initial build.
"""

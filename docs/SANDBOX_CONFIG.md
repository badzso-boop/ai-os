# Sandbox Configuration (`.ai-os/sandbox.json`)

This document describes the structure and configuration options for `.ai-os/sandbox.json` used by AI-OS sandbox runner environments.

## Risk-level overrides

Configuration in `.ai-os/sandbox.json` supports risk-level overrides under the `risks` section (such as `test_command` overrides under `risks.high` or `risks.critical`). When specified, these risk-specific configuration blocks allow overriding default settings tailored to the execution risk level.

The runner automatically merges the appropriate block based on the task's `risk` attribute before executing sandbox tasks. For instance, if a task has a `risk` attribute set to `high`, any override specified under `risks.high` (such as a custom `test_command`) is merged into the base sandbox configuration.
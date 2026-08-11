# Sandbox Configuration (`.ai-os/sandbox.json`)

This document describes the structure and configuration options for `.ai-os/sandbox.json` used by AI-OS sandbox runner environments.

## Risk-level overrides

Configuration in `.ai-os/sandbox.json` supports risk-specific configuration blocks under the `risks` section (such as `test_command` overrides under `risks.high` or `risks.critical`). When specified, these blocks allow overriding default sandbox settings tailored to the execution risk level of a given task.

The runner automatically merges the appropriate block based on the task's `risk` attribute before running the task sandbox. For example, if a task specifies a `risk` level of `high`, the settings under `risks.high` (e.g. customized `test_command` parameters) are merged into the base sandbox configuration.
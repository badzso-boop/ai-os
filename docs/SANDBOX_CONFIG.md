# Sandbox Configuration (`.ai-os/sandbox.json`)

The `.ai-os/sandbox.json` file configures the isolated execution environment for tasks, specifying environment settings, setup commands, test commands, build commands, and risk-level overrides.

## Configuration Structure

A standard `sandbox.json` file contains:

```json
{
  "environment": "python",
  "setup_commands": [
    "pip install -r requirements.txt"
  ],
  "test_command": "python -m pytest",
  "build_command": null,
  "timeout": 300,
  "env": {},
  "risks": {
    "high": {
      "test_command": "python -m pytest tests/integration tests/unit"
    },
    "critical": {
      "test_command": "python -m pytest --cov=ai_os tests/"
    }
  }
}
```

## Risk-level overrides

The runner supports risk-specific configuration blocks defined under the `risks` object in `.ai-os/sandbox.json` (such as `risks.high` or `risks.critical`).

### How Risk-Level Overrides Work

1. **Risk Attribute Evaluation**: Every task dispatched to the sandbox carries a `risk` attribute (e.g. `low`, `medium`, `high`, `critical`).
2. **Automatic Merging**: When executing a task, the runner inspects the task's `risk` level. If a matching block exists under `risks.<risk_level>` in `.ai-os/sandbox.json`, the runner automatically deep-merges that risk-specific configuration block over the default top-level sandbox configuration.
3. **Override Specificity**: Properties specified within `risks.high` or `risks.critical` (such as `test_command`, `timeout`, or specific `env` parameters) take precedence over the base sandbox configuration for tasks executed at that risk level. If a risk block does not specify a field, the base default value remains unchanged.
# Sandbox Configuration

The sandbox configuration file defines how the sandbox runner should behave for various tasks. Below is an overview of the configurable sections.

## Risk-level overrides

The runner can apply risk‑level specific overrides to the sandbox configuration. Define a top‑level `risks` map where each key is a risk level (e.g., `high`, `critical`) and the value is a partial configuration that will be merged over the base configuration when a task with that risk level is executed.

```json
{
  "base": {
    "timeout": 300,
    "max_cpu": 2,
    "max_memory": "4Gi"
  },
  "risks": {
    "high": {
      "timeout": 120,
      "max_cpu": 1
    },
    "critical": {
      "timeout": 60,
      "max_cpu": 0.5,
      "max_memory": "2Gi"
    }
  }
}
```

**How it works:** When a task is submitted, the sandbox runner looks at `task.risk`. It starts with the `base` configuration and then merges the corresponding block from the `risks` map (if present). The merge is shallow: any fields defined in the risk‑specific block override the same fields in `base`. This allows fine‑grained control of resources and limits per risk level.
"""CLI tests for `ai-os epic run` — monkeypatches the LLM-facing pieces
(`load_configured_adapters`, `decompose`, and the EpicRunner's actual
execution) so the plan-review gate and wiring are tested without real LLM
calls, Docker, or git."""
from __future__ import annotations

import json

from click.testing import CliRunner

import ai_os.cli as cli_module
from ai_os.core.epic_runner import EpicRunResult
from ai_os.core.models import TaskNode
from ai_os.core.scheduler import Assignment
from ai_os.mcp.adapters.base_adapter import BaseMCPAdapter, LLMTaskRequest, LLMTaskResponse, TokenUsage


class _FakeAdapter(BaseMCPAdapter):
    async def execute_task(self, request: LLMTaskRequest) -> LLMTaskResponse:  # pragma: no cover
        return LLMTaskResponse(task_id=request.task_id, provider="fake", model_name="m", generated_text="", usage=TokenUsage())


_PLAN = [
    TaskNode(id="T1", title="types", description="d", risk_level="LOW", target_files=["a.py"], write_set={"a.py"}),
    TaskNode(id="T2", title="svc", description="d", risk_level="HIGH", target_files=["b.py"], write_set={"b.py"}, dependencies=["T1"]),
]


def _patch_common(monkeypatch, tmp_path, executed_flag):
    # A real registered path so registry.resolve works, but scanning it must
    # be cheap — an empty dir is fine.
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(cli_module.registry, "resolve", lambda n: tmp_path)
    monkeypatch.setattr(cli_module, "load_configured_adapters", lambda: {"anthropic": _FakeAdapter()})
    # Isolate the accounting DB (Stage 3) to a throwaway home so the CLI never
    # writes to the real ~/.ai-os during tests.
    monkeypatch.setenv("AI_OS_HOME", str(tmp_path / "home"))

    async def fake_decompose(prompt, engine, adapter, model=None, conventions=""):
        return _PLAN

    monkeypatch.setattr(cli_module, "decompose", fake_decompose)

    async def fake_run_epic(self, tasks, epic_title="epic", raw_prompt=""):
        executed_flag["ran"] = True
        return EpicRunResult(completed=["T1", "T2"], task_results={}, epic_id="deadbeef",
                             assignments={t.id: Assignment("anthropic", "sonnet") for t in tasks})

    monkeypatch.setattr(cli_module.EpicRunner, "run_epic", fake_run_epic)
    # plan_assignments is called for the table; keep it real but harmless.


def test_epic_run_aborts_when_plan_declined(monkeypatch, tmp_path):
    flag = {"ran": False}
    _patch_common(monkeypatch, tmp_path, flag)
    result = CliRunner().invoke(
        cli_module.main, ["epic", "run", "proj", "--prompt", "do it", "--language", "python"], input="n\n"
    )
    assert result.exit_code == 0, result.output
    assert "Proposed DAG plan" in result.output
    assert "Aborted" in result.output
    assert flag["ran"] is False  # execution never happened


def test_epic_run_executes_when_approved(monkeypatch, tmp_path):
    flag = {"ran": False}
    _patch_common(monkeypatch, tmp_path, flag)
    result = CliRunner().invoke(
        cli_module.main, ["epic", "run", "proj", "--prompt", "do it", "--language", "python"], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert flag["ran"] is True
    assert "Completed" in result.output


def test_epic_run_yes_flag_skips_gate(monkeypatch, tmp_path):
    flag = {"ran": False}
    _patch_common(monkeypatch, tmp_path, flag)
    result = CliRunner().invoke(
        cli_module.main, ["epic", "run", "proj", "--prompt", "do it", "--language", "python", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert flag["ran"] is True


def test_epic_run_errors_without_providers(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module.registry, "resolve", lambda n: tmp_path)
    monkeypatch.setattr(cli_module, "load_configured_adapters", lambda: {})
    result = CliRunner().invoke(
        cli_module.main, ["epic", "run", "proj", "--prompt", "do it", "--language", "python"]
    )
    assert result.exit_code != 0
    assert "No LLM providers configured" in result.output


# -- ai-os cost / ai-os epic history (Stage 3 readback) ----------------------


def test_cost_and_history_read_back_persisted_spend(monkeypatch, tmp_path):
    import asyncio

    from ai_os.core.persistence import Persistence
    from ai_os.core.models import TaskNode as _TN
    from ai_os.mcp.adapters.base_adapter import TokenUsage as _TU

    home = tmp_path / "home"
    monkeypatch.setenv("AI_OS_HOME", str(home))

    async def _seed():
        # Uses the same default_db_url() the CLI resolves under AI_OS_HOME.
        from ai_os.core.persistence import default_db_url

        persistence, engine = await Persistence.open(default_db_url())
        try:
            await persistence.create_epic("E1", "add auth", "add JWT auth", status="COMPLETED")
            task = _TN(id="T1", title="t", description="d", risk_level="LOW", target_files=["a.py"], write_set={"a.py"})
            await persistence.upsert_task(task, "E1", assigned_model="sonnet", status="COMPLETED")
            await persistence.record_token_cost("T1", "anthropic", "sonnet", _TU(input_tokens=100, output_tokens=40, estimated_usd_cost=0.02))
        finally:
            await engine.dispose()

    asyncio.run(_seed())

    hist = CliRunner().invoke(cli_module.main, ["epic", "history"])
    assert hist.exit_code == 0, hist.output
    assert "add auth" in hist.output and "COMPLETED" in hist.output

    cost = CliRunner().invoke(cli_module.main, ["cost"])
    assert cost.exit_code == 0, cost.output
    assert "anthropic" in cost.output and "sonnet" in cost.output
    assert "0.0200" in cost.output

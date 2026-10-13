"""Unit tests for offline demo CLI."""

from __future__ import annotations

import json

import pytest

from memory_aware_agent_harness.cli import main


def test_cli_help() -> None:
    """CLI --help prints help message and exits with status 0."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_cli_simple_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI default simple scenario runs and exits with status 0."""
    exit_code = main(["--scenario", "simple"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "Agent Runtime Execution Result" in captured.out
    assert "Status:            COMPLETED" in captured.out
    assert "Task completed successfully" in captured.out


def test_cli_tool_scenario_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI tool scenario executes tool call and logs verbose lifecycle events."""
    exit_code = main(["--scenario", "tool", "-v"])
    assert exit_code == 0

    captured = capsys.readouterr()
    # Verbose logging goes to stderr
    assert "[*] RUN_STARTED:" in captured.err
    assert "[*] STEP_STARTED:" in captured.err
    assert "-> Invoking tool: calculator" in captured.err
    assert "<- Tool calculator finished" in captured.err
    assert "[*] RUN_TERMINATED:" in captured.err

    # Formatted summary goes to stdout
    assert "21 * 2 = 42" in captured.out
    assert "Steps Executed:    2" in captured.out


def test_cli_budget_exhaustion(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI budget scenario halts on limit breach and exits with status 2."""
    exit_code = main(["--scenario", "budget", "--max-steps", "2"])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "Status:            BUDGET_EXHAUSTED" in captured.out
    assert "Failure Code:      BUDGET_EXCEEDED" in captured.out


def test_cli_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --json outputs valid canonical JSON terminal RunResult."""
    exit_code = main(["--scenario", "simple", "--json"])
    assert exit_code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["status"] == "completed"
    assert "run_id" in data
    assert data["step_count"] == 1


def test_cli_custom_task(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI executes custom task instruction with custom namespace."""
    exit_code = main(["Analyze sales data", "--namespace", "analytics_ns"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "Namespace:         analytics_ns" in captured.out
    assert "Status:            COMPLETED" in captured.out


def test_cli_empty_task_error(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI handles empty task error gracefully and exits with status 1."""
    exit_code = main(["   "])
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "Error executing agent runtime:" in captured.err


def test_main_module_execution() -> None:
    """Invoking python -m memory_aware_agent_harness executes CLI."""
    import os
    import subprocess
    import sys

    env = dict(os.environ, PYTHONPATH="src")
    res = subprocess.run(
        [sys.executable, "-m", "memory_aware_agent_harness", "--scenario", "simple"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    assert "Agent Runtime Execution Result" in res.stdout


def test_cli_tool_observation_derivation_and_failure_handling() -> None:
    """CLI tool observation handler derives answer strictly from tool output without 42 fallback."""
    from memory_aware_agent_harness.cli import build_scenario_adapter
    from memory_aware_agent_harness.contracts import (
        ContextManifest,
        ModelCallContext,
        ModelFinishReason,
        NamespaceId,
        RunId,
        StepId,
    )

    adapter, _tools = build_scenario_adapter("tool")

    ctx = ModelCallContext(
        run_id=RunId("run_test"),
        step_id=StepId("step_test"),
        namespace_id=NamespaceId("ns_test"),
        budget_remaining_tokens=500,
    )

    # 1. Successful observation derivation with custom value
    manifest_ok = ContextManifest(
        manifest_id="m_ok",
        step_id=StepId("step_test"),
        included_items=(),
        excluded_item_ids={},
        total_tokens=10,
        budget_limit=1000,
        compiled_prompt='Observation (calculator, completed): {"result": 42.0}',
    )
    adapter1, _ = build_scenario_adapter("tool")
    _ = adapter1.decide(manifest_ok, [], ctx)  # consume step 1 tool call
    dec_ok = adapter1.decide(manifest_ok, [], ctx)
    assert dec_ok.finish_reason == ModelFinishReason.STOP
    assert dec_ok.content == "Calculation complete: 21 * 2 = 42."

    # 2. Missing observation returns ERROR without fallback
    manifest_missing = ContextManifest(
        manifest_id="m_missing",
        step_id=StepId("step_test"),
        included_items=(),
        excluded_item_ids={},
        total_tokens=10,
        budget_limit=1000,
        compiled_prompt="No tool observations in prompt",
    )
    adapter2, _ = build_scenario_adapter("tool")
    _ = adapter2.decide(manifest_missing, [], ctx)  # consume step 1 tool call
    dec_missing = adapter2.decide(manifest_missing, [], ctx)
    assert dec_missing.finish_reason == ModelFinishReason.ERROR
    assert dec_missing.failure_details is not None
    assert dec_missing.failure_details.error_code == "TOOL_OBSERVATION_FAILED"
    assert "42" not in (dec_missing.content or "")

    # 3. Failed tool observation returns ERROR without fallback
    manifest_failed = ContextManifest(
        manifest_id="m_failed",
        step_id=StepId("step_test"),
        included_items=(),
        excluded_item_ids={},
        total_tokens=10,
        budget_limit=1000,
        compiled_prompt='Observation (calculator, failed): {"error": "Device offline"}',
    )
    adapter3, _ = build_scenario_adapter("tool")
    _ = adapter3.decide(manifest_failed, [], ctx)  # consume step 1 tool call
    dec_failed = adapter3.decide(manifest_failed, [], ctx)
    assert dec_failed.finish_reason == ModelFinishReason.ERROR
    assert dec_failed.failure_details is not None
    assert dec_failed.failure_details.error_code == "TOOL_OBSERVATION_FAILED"
    assert "42" not in (dec_failed.content or "")

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

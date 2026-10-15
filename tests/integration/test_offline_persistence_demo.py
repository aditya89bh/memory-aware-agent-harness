"""Test for cross-process offline persistence demonstration script."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

try:
    from examples.offline_persistence_demo import CalculatorTool, main, run_offline_persistence_demo
except ModuleNotFoundError:
    _repo = str(Path(__file__).resolve().parent.parent.parent)
    if _repo not in sys.path and (Path(_repo) / "examples").is_dir():
        sys.path.insert(0, _repo)
        from examples.offline_persistence_demo import (
            CalculatorTool,
            main,
            run_offline_persistence_demo,
        )
    else:
        pytest.skip(
            "examples.offline_persistence_demo not importable in installed distribution",
            allow_module_level=True,
        )


def test_offline_persistence_demo_execution(tmp_path: Path) -> None:
    """Verifies that run_offline_persistence_demo runs cleanly and returns expected summary."""
    db_file = tmp_path / "test_demo.db"
    summary = run_offline_persistence_demo(db_file)

    assert summary["db_path"] == str(db_file)
    assert summary["run_id"] == "run_cross_proc_demo"
    assert summary["namespace_id"] == "demo_tenant"
    assert summary["checkpoint_id"].startswith("ckpt_")
    assert len(summary["state_hash"]) == 64
    assert summary["restored_step_count"] == 2
    assert summary["final_status"] == "completed"
    assert summary["trace_events_count"] > 0
    assert summary["process_a_pid"] != summary["process_b_pid"]
    assert summary["process_a_pid"] > 0
    assert summary["process_b_pid"] > 0


def test_offline_persistence_demo_default_temp() -> None:
    """Verifies demonstration with default temporary database path."""
    summary = run_offline_persistence_demo(None)
    assert Path(summary["db_path"]).exists()
    assert summary["final_status"] == "completed"


def test_offline_persistence_demo_cli_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies CLI entrypoint runs without errors."""
    db_file = tmp_path / "cli_demo.db"
    monkeypatch.setattr("sys.argv", ["offline_persistence_demo", "--db-path", str(db_file)])
    main()
    assert db_file.exists()


def test_calculator_tool_operations() -> None:
    """Verifies CalculatorTool schema and operations."""
    tool = CalculatorTool()
    schema = tool.get_schema()
    assert schema["type"] == "object"
    assert "op" in schema["properties"]
    # Verify non-add operation returns default result
    res = tool.execute({"op": "unknown"}, None)  # type: ignore[arg-type]
    assert res == {"result": 0}

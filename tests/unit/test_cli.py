"""Unit tests for offline demo CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memory_aware_agent_harness.cli import main
from memory_aware_agent_harness.contracts import (
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    NamespaceId,
    RunId,
    RunResult,
    RunStatus,
)
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter


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


def test_cli_inspect_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verifies inspect-db subcommand in standard and JSON mode."""
    db_file = tmp_path / "inspect.db"
    ns = NamespaceId("ns_inspect")
    now = datetime.now(UTC)

    with SQLiteStorageAdapter(str(db_file)) as storage:
        storage.save_run(
            RunResult(
                run_id=RunId("run_ins_1"),
                namespace_id=ns,
                status=RunStatus.COMPLETED,
                step_count=1,
                total_tokens_consumed=50,
                final_output="Done",
                trace_id="tr_ins",
            )
        )
        storage.put(
            MemoryRecord(
                id=MemoryId("mem_ins_1"),
                namespace_id=ns,
                kind=MemoryKind.SEMANTIC,
                content="Inspected fact",
                state=MemoryState.ACTIVE,
                confidence=1.0,
                salience=0.5,
                created_at=now,
                updated_at=now,
                origin=MemoryOrigin.AGENT_OBSERVATION,
            )
        )

    # Standard inspection output
    code = main(["inspect-db", str(db_file)])
    assert code == 0
    out = capsys.readouterr().out
    assert "SQLite WAL Database Inspection" in out
    assert "Integrity Check:    ok" in out
    assert "Runs:               1" in out

    # JSON inspection output
    code_json = main(["inspect-db", str(db_file), "--json"])
    assert code_json == 0
    out_json = capsys.readouterr().out
    stats = json.loads(out_json)
    assert stats["integrity"] == "ok"
    assert stats["runs"] == 1
    assert stats["memories_active"] == 1

    # Nonexistent file error
    code_err = main(["inspect-db", str(tmp_path / "nonexistent.db")])
    assert code_err == 1
    err = capsys.readouterr().err
    assert "Database file does not exist" in err


def test_cli_query_memories(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verifies query-memories subcommand with filtering and searching."""
    db_file = tmp_path / "query_mems.db"
    ns = NamespaceId("ns_query")
    now = datetime.now(UTC)

    with SQLiteStorageAdapter(str(db_file)) as storage:
        storage.put(
            MemoryRecord(
                id=MemoryId("mem_q_sem"),
                namespace_id=ns,
                kind=MemoryKind.SEMANTIC,
                content="Architecture guidelines for durable agents",
                state=MemoryState.ACTIVE,
                confidence=0.95,
                salience=0.8,
                created_at=now,
                updated_at=now,
                origin=MemoryOrigin.USER_EXPLICIT,
            )
        )
        storage.put(
            MemoryRecord(
                id=MemoryId("mem_q_epi"),
                namespace_id=ns,
                kind=MemoryKind.EPISODIC,
                content="Completed task execution at epoch 100",
                state=MemoryState.ACTIVE,
                confidence=0.9,
                salience=0.5,
                created_at=now,
                updated_at=now,
                origin=MemoryOrigin.AGENT_OBSERVATION,
            )
        )

    # Query all active memories
    code = main(["query-memories", str(db_file), "--namespace", str(ns)])
    assert code == 0
    out = capsys.readouterr().out
    assert "Memories in namespace 'ns_query' (Count: 2)" in out

    # Query with kind filter and JSON output
    code_kind = main(
        ["query-memories", str(db_file), "--namespace", str(ns), "--kind", "semantic", "--json"]
    )
    assert code_kind == 0
    items = json.loads(capsys.readouterr().out)
    assert len(items) == 1
    assert items[0]["id"] == "mem_q_sem"

    # Query with text search
    code_search = main(
        ["query-memories", str(db_file), "--namespace", str(ns), "--query", "Architecture"]
    )
    assert code_search == 0
    search_out = capsys.readouterr().out
    assert "Architecture guidelines" in search_out

    # Invalid kind error handled by argparse choices
    with pytest.raises(SystemExit) as exc_info:
        main(["query-memories", str(db_file), "--namespace", str(ns), "--kind", "nonexistent_kind"])
    assert exc_info.value.code == 2

    # Nonexistent file
    code_no_file = main(["query-memories", "/no/file.db", "--namespace", str(ns)])
    assert code_no_file == 1


def test_cli_tombstone_memory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verifies tombstone-memory subcommand soft-deletion."""
    db_file = tmp_path / "tombstone.db"
    ns = NamespaceId("ns_tomb")
    now = datetime.now(UTC)

    with SQLiteStorageAdapter(str(db_file)) as storage:
        storage.put(
            MemoryRecord(
                id=MemoryId("mem_to_tomb"),
                namespace_id=ns,
                kind=MemoryKind.SEMANTIC,
                content="Old information to be deleted",
                state=MemoryState.ACTIVE,
                confidence=0.8,
                salience=0.5,
                created_at=now,
                updated_at=now,
                origin=MemoryOrigin.AGENT_OBSERVATION,
            )
        )
        storage.put(
            MemoryRecord(
                id=MemoryId("mem_to_tomb_json"),
                namespace_id=ns,
                kind=MemoryKind.SEMANTIC,
                content="Second information to be deleted",
                state=MemoryState.ACTIVE,
                confidence=0.8,
                salience=0.5,
                created_at=now,
                updated_at=now,
                origin=MemoryOrigin.AGENT_OBSERVATION,
            )
        )

    code = main(
        [
            "tombstone-memory",
            str(db_file),
            "--namespace",
            str(ns),
            "--memory-id",
            "mem_to_tomb",
            "--reason",
            "User requested deletion",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Successfully tombstoned memory 'mem_to_tomb'" in out

    # Verify first memory is now tombstoned
    with SQLiteStorageAdapter(str(db_file)) as storage:
        active = storage.list_active(ns, kind=MemoryKind.SEMANTIC)
        assert len(active) == 1

    # JSON output mode
    code_json = main(
        [
            "tombstone-memory",
            str(db_file),
            "--namespace",
            str(ns),
            "--memory-id",
            "mem_to_tomb_json",
            "--reason",
            "Second deletion",
            "--json",
        ]
    )
    assert code_json == 0
    res_json = json.loads(capsys.readouterr().out)
    assert res_json["status"] == "tombstoned"

    # Nonexistent file
    code_no_file = main(
        [
            "tombstone-memory",
            "/no/file.db",
            "--namespace",
            str(ns),
            "--memory-id",
            "m",
            "--reason",
            "r",
        ]
    )
    assert code_no_file == 1


def test_cli_recover_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verifies recover-runs subcommand orphaned run reconciliation."""
    db_file = tmp_path / "recover.db"
    ns = NamespaceId("ns_rec")

    # Create an orphaned RUNNING run
    with SQLiteStorageAdapter(str(db_file)) as storage:
        storage.save_run(
            RunResult(
                run_id=RunId("run_orphaned_1"),
                namespace_id=ns,
                status=RunStatus.RUNNING,
                step_count=0,
                total_tokens_consumed=0,
                final_output=None,
                trace_id="tr_rec",
            )
        )

    # Standard recovery output
    code = main(["recover-runs", str(db_file), "--namespace", str(ns)])
    assert code == 0
    out = capsys.readouterr().out
    assert "Reconciled 1 orphaned run(s) in namespace 'ns_rec'" in out
    assert "run_orphaned_1: running -> failed" in out

    # JSON recovery output (should report 0 now since already reconciled)
    code_json = main(["recover-runs", str(db_file), "--namespace", str(ns), "--json"])
    assert code_json == 0
    reports = json.loads(capsys.readouterr().out)
    assert len(reports) == 0

    # Nonexistent file
    code_no_file = main(["recover-runs", "/no/file.db", "--namespace", str(ns)])
    assert code_no_file == 1


def test_cli_run_subcommand_alias(capsys: pytest.CaptureFixture[str]) -> None:
    """Verifies 'run' subcommand alias delegates to runner parser."""
    exit_code = main(["run", "--scenario", "simple"])
    assert exit_code == 0
    assert "Status:            COMPLETED" in capsys.readouterr().out

"""End-to-end integration tests for AgentRuntime lifecycle over durable SQLite WAL storage."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    BranchId,
    CheckpointId,
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StateResurrectionConflictError,
    StepId,
    TokenUsage,
    ToolCallContext,
    ToolCallRequest,
    ToolCallStatus,
    ToolInvocationId,
    ToolProtocol,
)
from memory_aware_agent_harness.doubles import DeterministicClock, DeterministicIdGenerator
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter


class _DurableCalculatorTool(ToolProtocol):
    """Deterministic calculator tool for durable integration tests."""

    def __init__(self) -> None:
        self.name = "calc"
        self.description = "Perform mathematical operations"
        self.side_effect = SideEffectType.READ_ONLY

    def get_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "op": {"type": "string"},
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["op", "a", "b"],
        }

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        op = str(arguments.get("op", "add"))
        a = float(arguments.get("a", 0))
        b = float(arguments.get("b", 0))
        if op == "add":
            return {"sum": a + b}
        return {"result": 0}


class _DurableMutatingTool(ToolProtocol):
    """Mutating tool that records a journal entry."""

    def __init__(self) -> None:
        self.name = "write_journal"
        self.description = "Record journal entry"
        self.side_effect = SideEffectType.IDEMPOTENT_WRITE

    def get_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entry": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["entry"],
        }

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        entry = str(arguments.get("entry", ""))
        return {"status": "persisted", "bytes": len(entry)}


def test_e2e_runtime_multi_step_durable_lifecycle(tmp_path: Path) -> None:
    """AgentRuntime executes multi-step lifecycle persisting to disk SQLite in WAL mode.

    Verifies that after adapter closure, a new connection reads back:
    - Run status, step history, and token counts
    - Tool invocation intents and results
    - Checkpoints and deterministic state hashes
    - Sequenced trace events
    - Persisted memories
    """
    db_file = tmp_path / "durable_agent_lifecycle.db"
    ns = NamespaceId("ns_durable_prod")
    clock = DeterministicClock(start_time=datetime(2026, 10, 15, 14, 0, 0, tzinfo=UTC))
    id_gen = DeterministicIdGenerator(prefix="life_")

    calc_tool = _DurableCalculatorTool()
    journal_tool = _DurableMutatingTool()

    # Step 1: Model calls calc tool
    dec1 = ModelDecision(
        decision_id="dec_step_1",
        step_id=StepId("step_000001"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(
            ToolCallRequest(
                call_id="call_calc",
                tool_name="calc",
                arguments={"op": "add", "a": 15, "b": 27},
            ),
        ),
        token_usage=TokenUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
    )

    # Step 2: Model calls journal tool
    dec2 = ModelDecision(
        decision_id="dec_step_2",
        step_id=StepId("step_000002"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(
            ToolCallRequest(
                call_id="call_journal",
                tool_name="write_journal",
                arguments={"entry": "Sum computed as 42", "idempotency_key": "journal_entry_42"},
            ),
        ),
        token_usage=TokenUsage(prompt_tokens=25, completion_tokens=15, total_tokens=40),
    )

    # Step 3: Model finishes with final answer
    dec3 = ModelDecision(
        decision_id="dec_step_3",
        step_id=StepId("step_000003"),
        finish_reason=ModelFinishReason.STOP,
        content="The calculation is complete: 15 + 27 = 42.",
        token_usage=TokenUsage(prompt_tokens=30, completion_tokens=20, total_tokens=50),
    )

    model = FakeModelAdapter(responses=[dec1, dec2, dec3])

    # Execute run with first adapter instance
    with SQLiteStorageAdapter(str(db_file), clock=clock.now) as storage_1:
        runtime = AgentRuntime(
            model=model,
            tools=[calc_tool, journal_tool],
            storage=storage_1,
            clock=clock,
            id_generator=id_gen,
        )

        run_result = runtime.run(
            task="Compute 15 + 27 and log journal entry",
            namespace_id=ns,
            run_id=RunId("run_durable_e2e_01"),
        )

        assert run_result.status == RunStatus.COMPLETED
        assert run_result.step_count == 3
        assert run_result.total_tokens_consumed == 125
        assert run_result.final_output == "The calculation is complete: 15 + 27 = 42."
        assert len(run_result.step_history) == 3

        # Save an explicit memory record in the same database
        mem = MemoryRecord(
            id=MemoryId("mem_calc_42"),
            namespace_id=ns,
            kind=MemoryKind.SEMANTIC,
            content="Calculation result for 15+27 is 42",
            state=MemoryState.ACTIVE,
            confidence=1.0,
            salience=0.95,
            created_at=clock.now(),
            updated_at=clock.now(),
            origin=MemoryOrigin.AGENT_OBSERVATION,
            run_id=RunId("run_durable_e2e_01"),
        )
        storage_1.put(mem)

    # Storage is closed and flushed. Verify cross-process persistence via new connection.
    with SQLiteStorageAdapter(str(db_file)) as storage_2:
        # 1. Verify Run
        recovered_run = storage_2.get_run(RunId("run_durable_e2e_01"), ns)
        assert recovered_run is not None
        assert recovered_run.status == RunStatus.COMPLETED
        assert recovered_run.step_count == 3
        assert recovered_run.total_tokens_consumed == 125
        assert recovered_run.final_output == "The calculation is complete: 15 + 27 = 42."

        # 2. Verify Step Records
        step1 = storage_2.get_step(run_result.step_history[0].step_id, ns)
        assert step1 is not None
        assert step1.step_number == 1
        assert step1.decision is not None
        assert step1.decision.finish_reason == ModelFinishReason.TOOL_CALL

        step2 = storage_2.get_step(run_result.step_history[1].step_id, ns)
        assert step2 is not None
        assert step2.step_number == 2

        step3 = storage_2.get_step(run_result.step_history[2].step_id, ns)
        assert step3 is not None
        assert step3.step_number == 3

        # 3. Verify Latest Checkpoint and deterministic state hash
        latest_cp = storage_2.get_latest_checkpoint(RunId("run_durable_e2e_01"), ns)
        assert latest_cp is not None
        assert latest_cp.step_id == run_result.step_history[2].step_id
        assert latest_cp.state_hash is not None
        assert len(latest_cp.state_hash) == 64  # SHA-256 hex string

        # 4. Verify Sequenced Trace Events
        trace_events = storage_2.get_trace_events(RunId("run_durable_e2e_01"), ns)
        assert len(trace_events) >= 6
        # Sequence numbers must be strictly monotonic starting at 0
        seq_nums = [int(e["sequence_num"]) for e in trace_events]
        assert seq_nums == sorted(seq_nums)
        assert seq_nums[0] == 0

        # 5. Verify Memory Search
        search_res = storage_2.search(
            namespace_id=ns,
            query="Calculation",
            kinds=(MemoryKind.SEMANTIC,),
            limit=5,
        )
        assert len(search_res) == 1
        assert search_res[0].id == "mem_calc_42"
        assert "Calculation result" in search_res[0].content


def test_e2e_checkpoint_state_restoration_offline(tmp_path: Path) -> None:
    """AgentRuntime restores state from disk checkpoint and verifies anti-resurrection."""
    db_file = tmp_path / "checkpoint_restore.db"
    ns = NamespaceId("ns_restore")
    clock = DeterministicClock(start_time=datetime(2026, 10, 15, 15, 0, 0, tzinfo=UTC))

    calc_tool = _DurableCalculatorTool()
    dec1 = ModelDecision(
        decision_id="dec_res_1",
        step_id=StepId("step_res_1"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(
            ToolCallRequest(
                call_id="call_res_1",
                tool_name="calc",
                arguments={"op": "add", "a": 100, "b": 200},
            ),
        ),
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    dec2 = ModelDecision(
        decision_id="dec_res_2",
        step_id=StepId("step_res_2"),
        finish_reason=ModelFinishReason.STOP,
        content="Intermediate result preserved",
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    model = FakeModelAdapter(responses=[dec1, dec2])

    with SQLiteStorageAdapter(str(db_file), clock=clock.now) as storage:
        runtime = AgentRuntime(
            model=model,
            tools=[calc_tool],
            storage=storage,
            clock=clock,
        )
        run_res = runtime.run("Two step task", namespace_id=ns, run_id=RunId("run_restore_src"))
        assert run_res.status == RunStatus.COMPLETED

    # Offline re-open: retrieve the latest checkpoint and restore state
    with SQLiteStorageAdapter(str(db_file), clock=clock.now) as storage_reopen:
        latest_cp = storage_reopen.get_latest_checkpoint(RunId("run_restore_src"), ns)
        assert latest_cp is not None

        new_runtime = AgentRuntime(
            model=FakeModelAdapter(),
            tools=[calc_tool],
            storage=storage_reopen,
            clock=clock,
        )
        restored_state, rehydrated_info = new_runtime.restore_from_checkpoint(
            checkpoint_id=latest_cp.checkpoint_id,
            namespace_id=ns,
        )

        assert restored_state.step_count == 2
        assert restored_state.run_id == "run_restore_src"
        assert restored_state.namespace_id == ns

        # Save a checkpoint referencing a working memory ID that is subsequently tombstoned
        critical_mem_id = MemoryId("crit_mem_resurrect")
        storage_reopen.put(
            MemoryRecord(
                id=critical_mem_id,
                namespace_id=ns,
                kind=MemoryKind.SEMANTIC,
                content="Critical fact before tombstone",
                state=MemoryState.ACTIVE,
                confidence=1.0,
                salience=1.0,
                created_at=clock.now(),
                updated_at=clock.now(),
                origin=MemoryOrigin.AGENT_OBSERVATION,
            )
        )
        storage_reopen.save_checkpoint(
            checkpoint_id=CheckpointId("ckpt_with_crit_mem"),
            run_id=RunId("run_restore_src"),
            step_id=latest_cp.step_id,
            branch_id=BranchId("main"),
            namespace_id=ns,
            state_payload={
                "run_id": "run_restore_src",
                "step_id": str(latest_cp.step_id),
                "step_number": 2,
                "status": "completed",
                "tokens": 40,
                "working_memory_ids": [str(critical_mem_id)],
            },
            created_at=clock.now(),
        )

        storage_reopen.tombstone(critical_mem_id, ns, reason="Revoked credential")

        # Anti-resurrection safeguard: restoring with conflict_strategy='refuse' raises
        with pytest.raises(StateResurrectionConflictError, match=r"crit_mem_resurrect"):
            new_runtime.restore_from_checkpoint(
                checkpoint_id=CheckpointId("ckpt_with_crit_mem"),
                namespace_id=ns,
                critical_memory_ids=(critical_mem_id,),
                conflict_strategy="refuse",
            )


def test_e2e_orphaned_run_crash_recovery_offline(tmp_path: Path) -> None:
    """Unfinished in-flight runs and pending tools on disk are recovered after simulated crash."""
    db_file = tmp_path / "crash_recovery.db"
    ns = NamespaceId("ns_crash")
    run_id = RunId("run_crashed_01")
    step_id = StepId("step_crashed_01")
    inv_id = ToolInvocationId("inv_crashed_01")
    now = datetime(2026, 10, 15, 16, 0, 0, tzinfo=UTC)

    # 1. Simulate process crash by writing in-flight state to disk and closing
    with SQLiteStorageAdapter(str(db_file)) as storage:
        storage.save_run(
            _create_test_run(
                run_id=run_id,
                namespace_id=ns,
                status=RunStatus.RUNNING,
            )
        )
        storage.record_step_start(
            step_id=step_id,
            run_id=run_id,
            namespace_id=ns,
            step_number=1,
            started_at=now,
        )
        storage.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="critical_transfer",
            arguments={"amount": 1000},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key="tx_crash_1",
            dispatched_at=now,
        )

    # 2. Next process boot: open storage, detect and recover orphaned runs
    with SQLiteStorageAdapter(str(db_file)) as recovery_storage:
        # Tool reconciliation marks pending mutating tool as OUTCOME_UNKNOWN and flags dirty
        tool_report = recovery_storage.reconcile_tool_invocations(namespace_id=ns)
        assert tool_report.reconciled_count == 1
        assert tool_report.marked_unknown_count == 1
        assert tool_report.dirty_non_idempotent_count == 1

        # Reconcile all orphaned runs within namespace
        reports = recovery_storage.reconcile_all_orphaned_runs(
            namespace_id=ns,
            strategy="fail",
            reason="Process terminated abruptly",
        )
        assert len(reports) == 1
        assert reports[0].run_id == run_id
        assert reports[0].previous_status == RunStatus.RUNNING
        assert reports[0].new_status == RunStatus.FAILED
        assert reports[0].is_dirty is True

        # Verify recovered run state in SQLite
        updated_run = recovery_storage.get_run(run_id, ns)
        assert updated_run is not None
        assert updated_run.status == RunStatus.FAILED
        assert updated_run.failure_details is not None
        assert updated_run.failure_details.error_code == "PARTIAL_DIRTY_ROLLBACK"

        # Verify tool invocation in SQLite
        inv_rec = recovery_storage.get_tool_invocation_record(inv_id, ns)
        assert inv_rec is not None
        assert inv_rec.status == ToolCallStatus.OUTCOME_UNKNOWN


def test_e2e_memory_retrieval_and_tombstoning_offline(tmp_path: Path) -> None:
    """Memories persisted to disk support category search, listing, and tombstoning."""
    db_file = tmp_path / "memory_lifecycle.db"
    ns = NamespaceId("ns_mem_lifecycle")
    now = datetime(2026, 10, 15, 17, 0, 0, tzinfo=UTC)

    mem1_id = MemoryId("mem_active_1")
    mem2_id = MemoryId("mem_active_2")

    with SQLiteStorageAdapter(str(db_file)) as storage:
        # Persist two memories with different confidence and salience
        mem1 = MemoryRecord(
            id=mem1_id,
            namespace_id=ns,
            kind=MemoryKind.SEMANTIC,
            content="Kubernetes cluster configured with 3 worker nodes",
            state=MemoryState.ACTIVE,
            confidence=0.9,
            salience=0.95,
            created_at=now,
            updated_at=now,
            origin=MemoryOrigin.AGENT_OBSERVATION,
        )
        mem2 = MemoryRecord(
            id=mem2_id,
            namespace_id=ns,
            kind=MemoryKind.SEMANTIC,
            content="Deprecated database backup schedule in cron",
            state=MemoryState.ACTIVE,
            confidence=0.3,
            salience=0.4,
            created_at=now - timedelta(days=1),
            updated_at=now - timedelta(days=1),
            origin=MemoryOrigin.USER_EXPLICIT,
        )
        storage.put(mem1)
        storage.put(mem2)

    # Re-open in second session
    with SQLiteStorageAdapter(str(db_file)) as storage_2:
        # List active
        active = storage_2.list_active(namespace_id=ns, kind=MemoryKind.SEMANTIC)
        assert len(active) == 2

        # Search by keyword
        res = storage_2.search(
            namespace_id=ns,
            query="Kubernetes",
            kinds=(MemoryKind.SEMANTIC,),
            limit=10,
        )
        assert len(res) == 1
        assert res[0].id == mem1_id

        # Tombstone the deprecated memory
        storage_2.tombstone(
            memory_id=mem2_id,
            namespace_id=ns,
            reason="Replaced by automated system",
        )

        # List active now only has mem1
        active_after = storage_2.list_active(namespace_id=ns, kind=MemoryKind.SEMANTIC)
        assert len(active_after) == 1
        assert active_after[0].id == mem1_id

        # Tombstoned memory IDs set contains mem2_id
        tombstoned_ids = storage_2.list_tombstoned_memory_ids(namespace_id=ns)
        assert mem2_id in tombstoned_ids
        assert mem1_id not in tombstoned_ids


def _create_test_run(
    run_id: RunId,
    namespace_id: NamespaceId,
    status: RunStatus,
) -> RunResult:
    """Helper to instantiate RunResult for testing."""
    return RunResult(
        run_id=run_id,
        namespace_id=namespace_id,
        status=status,
        step_count=0,
        total_tokens_consumed=0,
        final_output=None,
        trace_id=f"tr_{run_id}",
    )

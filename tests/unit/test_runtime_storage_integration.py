"""Unit tests for StoragePort wiring and durable step commit in AgentRuntime."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    BranchId,
    CancellationToken,
    CheckpointId,
    ExecutionCancelledError,
    MemoryFilter,
    MemoryId,
    MemoryKind,
    MemoryRecord,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunId,
    RunResult,
    RunStatus,
    ScoredMemoryItem,
    SideEffectType,
    StepCommitError,
    StepId,
    StepResult,
    StoragePort,
    TokenUsage,
    ToolCallRequest,
    ToolCallStatus,
    ToolInvocationId,
    ToolProtocol,
    ToolResult,
)
from memory_aware_agent_harness.doubles import DeterministicClock
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter


class MockTool(ToolProtocol):
    def __init__(
        self,
        name: str,
        side_effect: SideEffectType = SideEffectType.READ_ONLY,
        result: Any = None,
        should_raise: Exception | None = None,
    ) -> None:
        self.name = name
        self.description = f"Mock tool {name}"
        self.side_effect = side_effect
        self.result = result if result is not None else {"status": "ok"}
        self.should_raise = should_raise
        self.executed_calls: list[Mapping[str, Any]] = []

    def get_schema(self) -> Mapping[str, Any]:
        return {"type": "object", "properties": {"arg": {"type": "string"}}}

    def execute(self, arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
        self.executed_calls.append(arguments)
        if self.should_raise is not None:
            raise self.should_raise
        return dict(self.result)


def test_runtime_with_storage_successful_run() -> None:
    """Verify AgentRuntime persists initial run, steps, checkpoints, and final outcome to SQLite."""
    ns = NamespaceId("ns_integration")
    clock = DeterministicClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))

    model = FakeModelAdapter(
        responses=[
            ModelDecision(
                decision_id="dec_1",
                step_id=StepId("step_1"),
                finish_reason=ModelFinishReason.STOP,
                content="Task finished successfully",
                token_usage=TokenUsage(prompt_tokens=15, completion_tokens=10, total_tokens=25),
            )
        ]
    )

    with SQLiteStorageAdapter(":memory:", clock=clock.now) as storage:
        runtime = AgentRuntime(
            model=model,
            storage=storage,
            clock=clock,
        )

        result = runtime.run("Complete the assignment", namespace_id=ns)
        assert result.status == RunStatus.COMPLETED
        assert result.final_output == "Task finished successfully"
        assert result.step_count == 1
        assert result.total_tokens_consumed == 25

        # Verify run in SQLite storage
        stored_run = storage.get_run(result.run_id, ns)
        assert stored_run is not None
        assert stored_run.status == RunStatus.COMPLETED
        assert stored_run.step_count == 1
        assert stored_run.total_tokens_consumed == 25
        assert stored_run.final_output == "Task finished successfully"

        # Verify step in SQLite storage
        step_id = result.step_history[0].step_id
        stored_step = storage.get_step(step_id, ns)
        assert stored_step is not None
        assert stored_step.step_number == 1
        assert stored_step.decision.content == "Task finished successfully"
        assert stored_step.tokens_consumed == 25

        # Verify checkpoint in SQLite storage
        latest_ckpt = storage.get_latest_checkpoint(result.run_id, ns)
        assert latest_ckpt is not None
        assert latest_ckpt.step_id == step_id
        assert latest_ckpt.verify_integrity()
        payload = latest_ckpt.state_payload()
        assert payload["status"] == RunStatus.RUNNING.value
        assert payload["step_number"] == 1
        assert payload["tokens"] == 25

        # Verify trace events in SQLite audit log
        events = storage.get_trace_events(result.run_id, ns)
        assert len(events) >= 5
        event_types = [e["event_type"] for e in events]
        assert "run_started" in event_types
        assert "step_started" in event_types
        assert "step_completed" in event_types
        assert "run_terminated" in event_types


def test_runtime_with_storage_tool_dispatch_and_intent() -> None:
    """Verify tool intent is recorded before execution and result updated after execution."""
    ns = NamespaceId("ns_tool_storage")
    clock = DeterministicClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))

    tool = MockTool(
        name="payment_processor",
        side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
        result={"payment_id": "pay_123", "status": "settled"},
    )

    model = FakeModelAdapter(
        responses=[
            ModelDecision(
                decision_id="dec_1",
                step_id=StepId("step_1"),
                finish_reason=ModelFinishReason.TOOL_CALL,
                tool_calls=(
                    ToolCallRequest(
                        call_id="call_pay_1",
                        tool_name="payment_processor",
                        arguments={"amount": 100, "currency": "USD"},
                    ),
                ),
                token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            ),
            ModelDecision(
                decision_id="dec_2",
                step_id=StepId("step_2"),
                finish_reason=ModelFinishReason.STOP,
                content="Payment processed",
                token_usage=TokenUsage(prompt_tokens=8, completion_tokens=2, total_tokens=10),
            ),
        ]
    )

    with SQLiteStorageAdapter(":memory:", clock=clock.now) as storage:
        runtime = AgentRuntime(
            model=model,
            tools=[tool],
            storage=storage,
            clock=clock,
        )

        result = runtime.run("Pay invoice", namespace_id=ns)
        assert result.status == RunStatus.COMPLETED
        assert result.step_count == 2
        assert len(result.tool_results) == 1

        tool_res = result.tool_results[0]
        assert tool_res.status == ToolCallStatus.COMPLETED

        # Check durable tool invocation record in SQLite
        inv_rec = storage.get_tool_invocation_record(tool_res.invocation_id, ns)
        assert inv_rec is not None
        assert inv_rec.tool_name == "payment_processor"
        assert inv_rec.status == ToolCallStatus.COMPLETED
        assert inv_rec.side_effect == SideEffectType.NON_IDEMPOTENT_WRITE
        args = json.loads(inv_rec.arguments_json)
        assert args == {"amount": 100, "currency": "USD"}
        res = json.loads(inv_rec.result_json) if inv_rec.result_json else {}
        assert res == {"payment_id": "pay_123", "status": "settled"}
        assert inv_rec.dispatched_at is not None
        assert inv_rec.completed_at is not None


def test_runtime_with_storage_mutating_tool_cancellation() -> None:
    """Verify mutating tool cancellation records OUTCOME_UNKNOWN in durable SQLite storage."""
    ns = NamespaceId("ns_cancel_storage")
    clock = DeterministicClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))

    cancel_token = CancellationToken()

    class MutatingToolWithCancel(ToolProtocol):
        name = "wire_transfer"
        description = "Wire transfer tool"
        side_effect = SideEffectType.NON_IDEMPOTENT_WRITE

        def get_schema(self) -> Mapping[str, Any]:
            return {}

        def execute(self, arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
            cancel_token.cancel("User cancelled mid-transfer")
            raise ExecutionCancelledError("Transfer interrupted by user")

    model = FakeModelAdapter(
        responses=[
            ModelDecision(
                decision_id="dec_1",
                step_id=StepId("step_1"),
                finish_reason=ModelFinishReason.TOOL_CALL,
                tool_calls=(
                    ToolCallRequest(
                        call_id="call_wire_1",
                        tool_name="wire_transfer",
                        arguments={"amount": 5000},
                    ),
                ),
                token_usage=TokenUsage(prompt_tokens=8, completion_tokens=4, total_tokens=12),
            ),
        ]
    )

    with SQLiteStorageAdapter(":memory:", clock=clock.now) as storage:
        runtime = AgentRuntime(
            model=model,
            tools=[MutatingToolWithCancel()],
            storage=storage,
            clock=clock,
        )

        result = runtime.run(
            "Transfer money",
            namespace_id=ns,
            cancellation_token=cancel_token,
        )
        assert result.status == RunStatus.CANCELLED
        assert len(result.tool_results) == 1
        assert result.tool_results[0].status == ToolCallStatus.OUTCOME_UNKNOWN

        # Verify durable storage has OUTCOME_UNKNOWN
        inv = storage.get_tool_invocation_record(result.tool_results[0].invocation_id, ns)
        assert inv is not None
        assert inv.status == ToolCallStatus.OUTCOME_UNKNOWN
        assert "Transfer interrupted" in (inv.error_text or "")

        # Verify terminal run saved to SQLite
        stored_run = storage.get_run(result.run_id, ns)
        assert stored_run is not None
        assert stored_run.status == RunStatus.CANCELLED

        # Verify step saved in run_steps
        step_id = result.step_history[0].step_id
        stored_step = storage.get_step(step_id, ns)
        assert stored_step is not None


def test_runtime_with_storage_tool_not_found() -> None:
    """Verify missing tool is recorded as FAILED in durable storage."""
    ns = NamespaceId("ns_missing_tool")
    clock = DeterministicClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))

    model = FakeModelAdapter(
        responses=[
            ModelDecision(
                decision_id="dec_1",
                step_id=StepId("step_1"),
                finish_reason=ModelFinishReason.TOOL_CALL,
                tool_calls=(
                    ToolCallRequest(
                        call_id="call_bad_1",
                        tool_name="nonexistent_service",
                        arguments={},
                    ),
                ),
                token_usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            ),
            ModelDecision(
                decision_id="dec_2",
                step_id=StepId("step_2"),
                finish_reason=ModelFinishReason.STOP,
                content="Recovered after missing tool",
                token_usage=TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            ),
        ]
    )

    with SQLiteStorageAdapter(":memory:", clock=clock.now) as storage:
        runtime = AgentRuntime(
            model=model,
            storage=storage,
            clock=clock,
        )

        result = runtime.run("Call missing tool", namespace_id=ns)
        assert result.status == RunStatus.COMPLETED
        assert len(result.tool_results) == 1
        assert result.tool_results[0].status == ToolCallStatus.FAILED

        inv = storage.get_tool_invocation(result.tool_results[0].invocation_id, ns)
        assert inv is not None
        assert inv.status == ToolCallStatus.FAILED
        assert "not found" in (inv.error or "")


def test_runtime_with_storage_step_commit_failure() -> None:
    """Verify that if step commit fails, the run transitions to FAILED cleanly."""
    ns = NamespaceId("ns_commit_fail")
    clock = DeterministicClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))

    model = FakeModelAdapter(
        responses=[
            ModelDecision(
                decision_id="dec_1",
                step_id=StepId("step_1"),
                finish_reason=ModelFinishReason.STOP,
                content="Finished",
                token_usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            )
        ]
    )

    with SQLiteStorageAdapter(":memory:", clock=clock.now) as storage:
        # Patch commit_step_transaction to simulate a failure
        def _failing_commit(*args: Any, **kwargs: Any) -> Any:
            raise StepCommitError(
                "Simulated transaction failure",
                phase="PHASE_2_RUN_STATE",
                step_id="step_fail",
                run_id="run_fail",
                namespace_id=str(ns),
            )

        storage.commit_step_transaction = _failing_commit  # type: ignore[method-assign]

        runtime = AgentRuntime(
            model=model,
            storage=storage,
            clock=clock,
        )

        result = runtime.run("Test failure", namespace_id=ns)
        assert result.status == RunStatus.FAILED
        assert result.failure_details is not None
        assert "StepCommitError" in result.failure_details.error_type


def test_runtime_fallback_storage_port_without_atomic_commit() -> None:
    """Verify fallback when storage only implements standard StoragePort."""
    ns = NamespaceId("ns_port_only")
    clock = DeterministicClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))

    class MinimalStoragePort(StoragePort):
        def __init__(self) -> None:
            self.runs: dict[str, RunResult] = {}
            self.steps: dict[str, StepResult] = {}
            self.checkpoints: dict[str, Mapping[str, Any]] = {}
            self.tool_intents: list[str] = []
            self.tool_results: list[ToolResult] = []
            self.trace_events: list[dict[str, Any]] = []

        def put(self, record: MemoryRecord) -> None:
            pass

        def get(self, memory_id: MemoryId, namespace_id: NamespaceId) -> MemoryRecord | None:
            return None

        def search(
            self, namespace_id: NamespaceId, query: str, kinds: Any, limit: int
        ) -> Sequence[MemoryRecord]:
            return ()

        def search_scored(self, query_filter: MemoryFilter) -> Sequence[ScoredMemoryItem]:
            return ()

        def list_active(
            self, namespace_id: NamespaceId, kind: MemoryKind
        ) -> Sequence[MemoryRecord]:
            return ()

        def tombstone(self, memory_id: MemoryId, namespace_id: NamespaceId, reason: str) -> None:
            pass

        def list_tombstoned_memory_ids(self, namespace_id: NamespaceId) -> frozenset[MemoryId]:
            return frozenset()

        def update_optimistic(self, record: MemoryRecord, expected_version: int) -> None:
            pass

        def save_run(self, run_result: RunResult) -> None:
            self.runs[str(run_result.run_id)] = run_result

        def get_run(self, run_id: RunId, namespace_id: NamespaceId) -> RunResult | None:
            return self.runs.get(str(run_id))

        def list_orphaned_runs(self, namespace_id: NamespaceId) -> Sequence[RunResult]:
            return [
                r
                for r in self.runs.values()
                if r.namespace_id == namespace_id
                and r.status in (RunStatus.RUNNING, RunStatus.INITIALIZED)
            ]

        def save_step(self, step_result: StepResult, namespace_id: NamespaceId) -> None:
            self.steps[str(step_result.step_id)] = step_result

        def record_step_start(
            self,
            step_id: StepId,
            run_id: RunId,
            namespace_id: NamespaceId,
            step_number: int,
            started_at: datetime,
        ) -> None:
            pass

        def record_tool_intent(
            self,
            invocation_id: ToolInvocationId,
            step_id: StepId,
            namespace_id: NamespaceId,
            tool_name: str,
            arguments: Mapping[str, Any],
            side_effect: SideEffectType,
            idempotency_key: str,
            dispatched_at: datetime,
        ) -> None:
            self.tool_intents.append(str(invocation_id))

        def record_tool_result(
            self,
            result: ToolResult,
            namespace_id: NamespaceId,
            *,
            completed_at: datetime | None = None,
        ) -> None:
            self.tool_results.append(result)

        def get_tool_invocation(
            self, invocation_id: ToolInvocationId, namespace_id: NamespaceId
        ) -> ToolResult | None:
            for r in self.tool_results:
                if r.invocation_id == invocation_id:
                    return r
            return None

        def save_checkpoint(
            self,
            checkpoint_id: CheckpointId,
            run_id: RunId,
            step_id: StepId,
            branch_id: BranchId,
            namespace_id: NamespaceId,
            state_payload: Mapping[str, Any],
            created_at: datetime,
            parent_checkpoint_id: CheckpointId | None = None,
        ) -> None:
            self.checkpoints[str(checkpoint_id)] = state_payload

        def get_checkpoint(
            self, checkpoint_id: CheckpointId, namespace_id: NamespaceId
        ) -> Mapping[str, Any] | None:
            return self.checkpoints.get(str(checkpoint_id))

        def append_trace_event(
            self,
            event_id: Any,
            run_id: RunId,
            namespace_id: NamespaceId,
            sequence_num: int,
            event_type: str,
            payload: Mapping[str, Any],
            emitted_at: datetime,
            step_id: StepId | None = None,
        ) -> None:
            self.trace_events.append({"event_type": event_type, "seq": sequence_num})

        def get_trace_events(
            self, run_id: RunId, namespace_id: NamespaceId
        ) -> Sequence[Mapping[str, Any]]:
            return tuple(self.trace_events)

    minimal_storage = MinimalStoragePort()
    model = FakeModelAdapter(
        responses=[
            ModelDecision(
                decision_id="dec_1",
                step_id=StepId("step_1"),
                finish_reason=ModelFinishReason.STOP,
                content="Standard finish",
                token_usage=TokenUsage(prompt_tokens=6, completion_tokens=4, total_tokens=10),
            )
        ]
    )

    runtime = AgentRuntime(
        model=model,
        storage=minimal_storage,
        clock=clock,
    )

    result = runtime.run("Run with minimal storage", namespace_id=ns)
    assert result.status == RunStatus.COMPLETED
    assert str(result.run_id) in minimal_storage.runs
    assert len(minimal_storage.steps) == 1
    assert len(minimal_storage.checkpoints) == 1
    assert len(minimal_storage.trace_events) >= 4


def test_runtime_storage_automatic_memory_store_resolution() -> None:
    """Verify passing storage automatically sets memory_store when not provided."""
    clock = DeterministicClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))
    model = FakeModelAdapter()

    with SQLiteStorageAdapter(":memory:", clock=clock.now) as storage:
        runtime = AgentRuntime(
            model=model,
            storage=storage,
            clock=clock,
        )
        assert runtime.memory_store is storage


def test_runtime_backward_compatibility_without_storage() -> None:
    """Verify runtime works normally when storage is None."""
    clock = DeterministicClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))
    model = FakeModelAdapter(
        responses=[
            ModelDecision(
                decision_id="dec_1",
                step_id=StepId("step_1"),
                finish_reason=ModelFinishReason.STOP,
                content="Pure in-memory run",
                token_usage=TokenUsage(prompt_tokens=4, completion_tokens=4, total_tokens=8),
            )
        ]
    )
    runtime = AgentRuntime(model=model, clock=clock)
    result = runtime.run("Pure in memory", namespace_id=NamespaceId("ns_no_storage"))
    assert result.status == RunStatus.COMPLETED
    assert result.final_output == "Pure in-memory run"
    assert runtime.storage is None

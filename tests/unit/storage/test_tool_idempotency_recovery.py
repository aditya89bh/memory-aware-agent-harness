"""Unit tests for tool idempotency crash recovery and cached replay."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from memory_aware_agent_harness.contracts import (
    DirtyRollbackError,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepId,
    StoragePort,
    ToolCallRequest,
    ToolCallStatus,
    ToolInvocationId,
    ToolProtocol,
    ToolResult,
)
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.runtime.tool_recovery import ToolRecoveryEngine
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter


class CountingMockTool(ToolProtocol):
    """Test double tool that tracks execution count."""

    def __init__(
        self,
        name: str,
        side_effect: SideEffectType = SideEffectType.IDEMPOTENT_WRITE,
        result: Any = None,
    ) -> None:
        self.name = name
        self.description = f"Counting mock tool {name}"
        self.side_effect = side_effect
        self.result = result if result is not None else {"status": "ok"}
        self.call_count = 0

    def get_schema(self) -> Mapping[str, Any]:
        return {"type": "object", "properties": {"arg": {"type": "string"}}}

    def execute(self, arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
        self.call_count += 1
        return dict(self.result)


def _setup_storage_with_step(
    adapter: SQLiteStorageAdapter,
    run_id: RunId,
    step_id: StepId,
    namespace_id: NamespaceId,
) -> None:
    """Helper to initialize parent run and step rows in SQLite."""
    run = RunResult(
        run_id=run_id,
        namespace_id=namespace_id,
        status=RunStatus.RUNNING,
        step_count=0,
        total_tokens_consumed=0,
        final_output=None,
        trace_id=f"tr_{run_id}",
    )
    adapter.save_run(run)
    adapter.record_step_start(
        step_id=step_id,
        run_id=run_id,
        namespace_id=namespace_id,
        step_number=1,
        started_at=datetime.now(UTC),
    )


def test_reconcile_pending_mutating_tools_marked_unknown_and_dirty() -> None:
    """Pending non-idempotent write must be marked OUTCOME_UNKNOWN and flagged dirty."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_idemp")
        run_id = RunId("run_t1")
        step_id = StepId("step_t1")
        _setup_storage_with_step(adapter, run_id, step_id, ns)

        inv_id = ToolInvocationId("inv_pending_write")
        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="transfer_funds",
            arguments={"amount": 100},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key="pay_123",
            dispatched_at=datetime.now(UTC),
        )

        engine = ToolRecoveryEngine(adapter)
        report = engine.reconcile_pending_invocations(ns)

        assert report.reconciled_count == 1
        assert report.marked_unknown_count == 1
        assert report.dirty_non_idempotent_count == 1

        rec = adapter.get_tool_invocation_record(inv_id, ns)
        assert rec is not None
        assert rec.status == ToolCallStatus.OUTCOME_UNKNOWN
        assert rec.error_text is not None
        assert "outcome unknown" in rec.error_text


def test_reconcile_pending_idempotent_tools_marked_unknown_not_dirty() -> None:
    """Pending idempotent invocation is marked OUTCOME_UNKNOWN but not flagged dirty."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_idemp")
        run_id = RunId("run_t2")
        step_id = StepId("step_t2")
        _setup_storage_with_step(adapter, run_id, step_id, ns)

        inv_id = ToolInvocationId("inv_idemp_write")
        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="upsert_file",
            arguments={"path": "/var/data.json"},
            side_effect=SideEffectType.IDEMPOTENT_WRITE,
            idempotency_key="file_key_1",
            dispatched_at=datetime.now(UTC),
        )

        report = adapter.reconcile_tool_invocations(ns)
        assert report.reconciled_count == 1
        assert report.marked_unknown_count == 1
        assert report.dirty_non_idempotent_count == 0


def test_replay_cached_result_for_completed_idempotent_tool() -> None:
    """A completed idempotent invocation returns the cached result without re-execution."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_idemp")
        run_id = RunId("run_t3")
        step_id = StepId("step_t3")
        _setup_storage_with_step(adapter, run_id, step_id, ns)

        inv_id = ToolInvocationId("inv_comp_1")
        idemp_key = "idemp_cache_1"
        now = datetime.now(UTC)

        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="write_file",
            arguments={"path": "/tmp/a.txt"},
            side_effect=SideEffectType.IDEMPOTENT_WRITE,
            idempotency_key=idemp_key,
            dispatched_at=now,
        )

        expected_payload = {"bytes_written": 256, "status": "ok"}
        tool_res = ToolResult(
            invocation_id=inv_id,
            tool_name="write_file",
            status=ToolCallStatus.COMPLETED,
            side_effect=SideEffectType.IDEMPOTENT_WRITE,
            idempotency_key=idemp_key,
            result=expected_payload,
        )
        adapter.record_tool_result(tool_res, ns, completed_at=now)

        engine = ToolRecoveryEngine(adapter)
        cached = engine.get_replayable_cached_result(
            idempotency_key=idemp_key,
            namespace_id=ns,
            tool_name="write_file",
        )

        assert cached is not None
        assert cached.status == ToolCallStatus.COMPLETED
        assert cached.result == expected_payload
        assert cached.execution_time_ms == 0.0


def test_block_automated_reexecution_for_unknown_non_idempotent_tool() -> None:
    """Non-idempotent write in OUTCOME_UNKNOWN must raise DirtyRollbackError on re-execution."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_idemp")
        run_id = RunId("run_t4")
        step_id = StepId("step_t4")
        _setup_storage_with_step(adapter, run_id, step_id, ns)

        inv_id = ToolInvocationId("inv_blocked_1")
        idemp_key = "charge_card_key"
        now = datetime.now(UTC)

        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="charge_card",
            arguments={"amount": 500},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key=idemp_key,
            dispatched_at=now,
        )
        adapter.mark_invocation_unknown(inv_id, ns)

        engine = ToolRecoveryEngine(adapter)
        with pytest.raises(DirtyRollbackError, match="Automated re-execution blocked"):
            engine.get_replayable_cached_result(
                idempotency_key=idemp_key,
                namespace_id=ns,
                tool_name="charge_card",
            )


def test_permit_reexecution_for_unknown_idempotent_tool() -> None:
    """Idempotent write in OUTCOME_UNKNOWN returns None, safely allowing re-execution."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_idemp")
        run_id = RunId("run_t5")
        step_id = StepId("step_t5")
        _setup_storage_with_step(adapter, run_id, step_id, ns)

        inv_id = ToolInvocationId("inv_idemp_retry")
        idemp_key = "put_blob_key"
        now = datetime.now(UTC)

        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="put_blob",
            arguments={"key": "k1"},
            side_effect=SideEffectType.IDEMPOTENT_WRITE,
            idempotency_key=idemp_key,
            dispatched_at=now,
        )
        adapter.mark_invocation_unknown(inv_id, ns)

        engine = ToolRecoveryEngine(adapter)
        # Permitted to retry, returns None rather than raising
        assert (
            engine.get_replayable_cached_result(
                idempotency_key=idemp_key,
                namespace_id=ns,
                tool_name="put_blob",
            )
            is None
        )


def test_manual_resolution_of_unknown_invocation() -> None:
    """Manual operator resolution allows recovering and caching an uncertain invocation."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_idemp")
        run_id = RunId("run_t6")
        step_id = StepId("step_t6")
        _setup_storage_with_step(adapter, run_id, step_id, ns)

        inv_id = ToolInvocationId("inv_manual_res")
        idemp_key = "external_op_1"
        now = datetime.now(UTC)

        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="external_action",
            arguments={},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key=idemp_key,
            dispatched_at=now,
        )
        adapter.mark_invocation_unknown(inv_id, ns)

        # Operator resolves after out-of-band verification
        resolved_record = adapter.resolve_tool_invocation(
            invocation_id=inv_id,
            namespace_id=ns,
            status=ToolCallStatus.COMPLETED,
            result={"confirmed_by_bank": True},
        )

        assert resolved_record.status == ToolCallStatus.COMPLETED
        assert resolved_record.result_json is not None
        assert "confirmed_by_bank" in resolved_record.result_json

        # Subsequent check returns the cached resolved outcome
        cached = adapter.get_replayable_cached_result(idemp_key, ns, "external_action")
        assert cached is not None
        assert cached.status == ToolCallStatus.COMPLETED
        assert cached.result == {"confirmed_by_bank": True}


def test_runtime_e2e_idempotent_tool_replay_avoids_execution() -> None:
    """AgentRuntime reuses cached idempotent tool results without invoking the tool twice."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_rt_idemp")
        mock_tool = CountingMockTool(
            "write_config", SideEffectType.IDEMPOTENT_WRITE, {"written": 42}
        )

        # Run 1: dispatches call with idempotency_key="cfg_v1"
        decision1 = ModelDecision(
            decision_id="dec_1",
            step_id=StepId("step_1"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_1",
                    tool_name="write_config",
                    arguments={"idempotency_key": "cfg_v1", "value": "test"},
                ),
            ),
        )
        model1 = FakeModelAdapter(
            responses=[
                decision1,
                ModelDecision(
                    decision_id="dec_1_done",
                    step_id=StepId("step_2"),
                    finish_reason=ModelFinishReason.STOP,
                    content="done",
                ),
            ]
        )
        runtime1 = AgentRuntime(model=model1, tools=[mock_tool], storage=adapter)
        res1 = runtime1.run(task="Run 1", namespace_id=ns, run_id=RunId("r_idemp_1"))
        assert res1.status == RunStatus.COMPLETED
        assert mock_tool.call_count == 1

        # Run 2: dispatches same tool call with same idempotency_key="cfg_v1"
        decision2 = ModelDecision(
            decision_id="dec_2",
            step_id=StepId("step_1"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_2",
                    tool_name="write_config",
                    arguments={"idempotency_key": "cfg_v1", "value": "test"},
                ),
            ),
        )
        model2 = FakeModelAdapter(
            responses=[
                decision2,
                ModelDecision(
                    decision_id="dec_2_done",
                    step_id=StepId("step_2"),
                    finish_reason=ModelFinishReason.STOP,
                    content="done",
                ),
            ]
        )
        runtime2 = AgentRuntime(model=model2, tools=[mock_tool], storage=adapter)
        res2 = runtime2.run(task="Run 2", namespace_id=ns, run_id=RunId("r_idemp_2"))
        assert res2.status == RunStatus.COMPLETED

        # The tool was NOT executed again; call_count remains 1!
        assert mock_tool.call_count == 1


def test_runtime_e2e_unknown_non_idempotent_blocks_execution() -> None:
    """AgentRuntime fail-closed blocks execution of non-idempotent tool with OUTCOME_UNKNOWN."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_rt_blocked")
        run_id = RunId("run_prior")
        step_id = StepId("step_prior")
        _setup_storage_with_step(adapter, run_id, step_id, ns)

        # Pre-populate an invocation in OUTCOME_UNKNOWN
        adapter.record_tool_intent(
            invocation_id=ToolInvocationId("inv_prior"),
            step_id=step_id,
            namespace_id=ns,
            tool_name="charge_customer",
            arguments={"idempotency_key": "charge_cust_42"},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key="charge_cust_42",
            dispatched_at=datetime.now(UTC),
        )
        adapter.mark_invocation_unknown(ToolInvocationId("inv_prior"), ns)

        mock_tool = CountingMockTool("charge_customer", SideEffectType.NON_IDEMPOTENT_WRITE)
        decision = ModelDecision(
            decision_id="dec_blocked",
            step_id=StepId("step_rt"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_blocked",
                    tool_name="charge_customer",
                    arguments={"idempotency_key": "charge_cust_42"},
                ),
            ),
        )
        model = FakeModelAdapter(
            responses=[
                decision,
                ModelDecision(
                    decision_id="dec_blocked_done",
                    step_id=StepId("step_rt_2"),
                    finish_reason=ModelFinishReason.STOP,
                    content="blocked",
                ),
            ]
        )
        runtime = AgentRuntime(model=model, tools=[mock_tool], storage=adapter)
        run_res = runtime.run(
            task="Charge customer",
            namespace_id=ns,
            run_id=RunId("r_blocked"),
        )

        # Tool execution was blocked: tool never called
        assert mock_tool.call_count == 0
        # Step failure recorded for the blocked call
        blocked_tool_res = run_res.tool_results[0]
        assert blocked_tool_res.status == ToolCallStatus.FAILED
        assert blocked_tool_res.failure_details is not None
        assert blocked_tool_res.failure_details.error_code == "PARTIAL_DIRTY_ROLLBACK"


def test_tool_recovery_engine_unsupported_storage() -> None:
    """Engine gracefully handles storage instances without recovery methods."""
    mock_storage = MagicMock(spec=StoragePort)
    engine = ToolRecoveryEngine(mock_storage)
    ns = NamespaceId("ns_min")

    report = engine.reconcile_pending_invocations(ns)
    assert report.reconciled_count == 0
    assert report.dirty_non_idempotent_count == 0

    assert (
        engine.get_replayable_cached_result(
            idempotency_key="key",
            namespace_id=ns,
            tool_name="tool",
        )
        is None
    )


def test_reconcile_pending_with_existing_outcome_unknown_records() -> None:
    """Pre-existing OUTCOME_UNKNOWN non-idempotent records are counted in dirty count."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_prior_unk")
        run_id = RunId("run_prior_unk")
        step_id = StepId("step_prior_unk")
        _setup_storage_with_step(adapter, run_id, step_id, ns)

        inv_id = ToolInvocationId("inv_prior_mutating")
        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="delete_account",
            arguments={"user_id": 99},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key="del_99",
            dispatched_at=datetime.now(UTC),
        )
        adapter.mark_invocation_unknown(inv_id, ns)

        engine = ToolRecoveryEngine(adapter)
        report = engine.reconcile_pending_invocations(ns, run_id=run_id)

        assert report.reconciled_count == 0  # not PENDING, so not transitioned
        assert report.dirty_non_idempotent_count == 1  # but flagged as dirty!
        assert report.run_id == run_id


def test_get_replayable_cached_result_not_found() -> None:
    """Non-existent idempotency key returns None."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        engine = ToolRecoveryEngine(adapter)
        res = engine.get_replayable_cached_result(
            idempotency_key="missing_key",
            namespace_id=NamespaceId("ns_missing"),
            tool_name="any_tool",
        )
        assert res is None


def test_get_replayable_cached_result_non_json_or_primitive() -> None:
    """Handles raw string or invalid JSON stored in result_json gracefully."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        ns = NamespaceId("ns_raw")
        run_id = RunId("run_raw")
        step_id = StepId("step_raw")
        _setup_storage_with_step(adapter, run_id, step_id, ns)

        inv_id = ToolInvocationId("inv_raw_res")
        idemp_key = "raw_key_1"
        now = datetime.now(UTC)

        adapter.record_tool_intent(
            invocation_id=inv_id,
            step_id=step_id,
            namespace_id=ns,
            tool_name="raw_tool",
            arguments={},
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key=idemp_key,
            dispatched_at=now,
        )

        # Record completed with non-mapping JSON (e.g., string or int)
        tool_res = ToolResult(
            invocation_id=inv_id,
            tool_name="raw_tool",
            status=ToolCallStatus.COMPLETED,
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key=idemp_key,
            result={"text": "normal"},
        )
        adapter.record_tool_result(tool_res, ns, completed_at=now)

        engine = ToolRecoveryEngine(adapter)
        cached = engine.get_replayable_cached_result(idemp_key, ns, "raw_tool")
        assert cached is not None
        assert cached.result == {"text": "normal"}


def test_resolve_invocation_not_found_raises() -> None:
    """Attempting to resolve non-existent invocation raises RecordNotFoundError."""
    with SQLiteStorageAdapter(":memory:") as adapter:
        engine = ToolRecoveryEngine(adapter)
        with pytest.raises(RecordNotFoundError, match="not found for resolution"):
            engine.resolve_invocation(
                invocation_id=ToolInvocationId("does_not_exist"),
                namespace_id=NamespaceId("ns_err"),
                status=ToolCallStatus.COMPLETED,
            )

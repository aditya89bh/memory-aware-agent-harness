"""Unit tests for orphaned run detection, dirty rollback, and crash recovery."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memory_aware_agent_harness.contracts import (
    CrossNamespaceAccessError,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepId,
    ToolCallStatus,
    ToolInvocationId,
)
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.recovery import (
    DirtyRollbackError,
    OrphanedRunReport,
)
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter


def _create_test_run(
    adapter: SQLiteStorageAdapter,
    run_id: RunId,
    namespace_id: NamespaceId,
    status: RunStatus = RunStatus.RUNNING,
) -> RunResult:
    """Helper to persist a test run."""
    run_result = RunResult(
        run_id=run_id,
        namespace_id=namespace_id,
        status=status,
        step_count=0,
        total_tokens_consumed=0,
        final_output=None,
        trace_id=f"trace_{run_id}",
    )
    adapter.save_run(run_result)
    return run_result


def test_clean_orphaned_run_reconcile_to_failed() -> None:
    """A clean interrupted run without mutating tools should fail with ORPHANED_RUN_DETECTED."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns = NamespaceId("ns_recovery")
    run_id = RunId("run_clean_1")
    _create_test_run(adapter, run_id, ns, status=RunStatus.RUNNING)

    orphaned = adapter.list_orphaned_runs(ns)
    assert len(orphaned) == 1
    assert orphaned[0].run_id == run_id

    report = adapter.reconcile_orphaned_run(run_id, ns, strategy="fail")

    assert isinstance(report, OrphanedRunReport)
    assert report.run_id == run_id
    assert report.namespace_id == ns
    assert report.previous_status == RunStatus.RUNNING
    assert report.new_status == RunStatus.FAILED
    assert report.is_dirty is False
    assert report.mutating_tool_invocations_count == 0
    assert report.failure_details.error_code == "ORPHANED_RUN_DETECTED"
    assert report.failure_details.error_type == "OrphanedRunError"

    # Verify persisted DB state
    updated_run = adapter.get_run(run_id, ns)
    assert updated_run is not None
    assert updated_run.status == RunStatus.FAILED
    assert updated_run.failure_details is not None
    assert updated_run.failure_details.error_code == "ORPHANED_RUN_DETECTED"

    # No orphaned runs remain
    assert adapter.list_orphaned_runs(ns) == ()


def test_clean_orphaned_run_reconcile_to_paused() -> None:
    """A clean interrupted run reconciled to PAUSED should record ORPHANED_RUN_PAUSED."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns = NamespaceId("ns_recovery")
    run_id = RunId("run_clean_paused")
    _create_test_run(adapter, run_id, ns, status=RunStatus.RUNNING)

    report = adapter.reconcile_orphaned_run(
        run_id, ns, strategy="pause", reason="Awaiting manual inspection"
    )

    assert report.new_status == RunStatus.PAUSED
    assert report.is_dirty is False
    assert report.failure_details.error_code == "ORPHANED_RUN_PAUSED"
    assert "Awaiting manual inspection" in report.reason

    updated_run = adapter.get_run(run_id, ns)
    assert updated_run is not None
    assert updated_run.status == RunStatus.PAUSED
    assert updated_run.failure_details is not None
    assert updated_run.failure_details.error_code == "ORPHANED_RUN_PAUSED"


def test_dirty_orphaned_run_with_mutating_tool_pending() -> None:
    """An interrupted run with in-flight mutating tool must trigger PARTIAL_DIRTY_ROLLBACK."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns = NamespaceId("ns_recovery")
    run_id = RunId("run_dirty_1")
    step_id = StepId("step_dirty_1")
    now = datetime.now(UTC)

    _create_test_run(adapter, run_id, ns, status=RunStatus.RUNNING)
    adapter.record_step_start(
        step_id=step_id,
        run_id=run_id,
        namespace_id=ns,
        step_number=1,
        started_at=now,
    )

    # Record in-flight mutating tool intent
    inv_id = ToolInvocationId("inv_mutating_1")
    adapter.record_tool_intent(
        invocation_id=inv_id,
        step_id=step_id,
        namespace_id=ns,
        tool_name="database_mutate",
        arguments={"action": "delete_customer", "id": 42},
        side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
        idempotency_key="idemp_mutating_1",
        dispatched_at=now,
    )

    # Invocations are pending
    pending = adapter.find_pending_or_unknown_invocations(ns, run_id=run_id)
    assert len(pending) == 1
    assert pending[0].status == ToolCallStatus.PENDING

    # Reconcile dirty run
    report = adapter.reconcile_orphaned_run(run_id, ns, strategy="fail")

    assert report.is_dirty is True
    assert report.mutating_tool_invocations_count == 1
    assert report.reconciled_steps_count == 1
    assert report.new_status == RunStatus.FAILED
    assert report.failure_details.error_code == "PARTIAL_DIRTY_ROLLBACK"
    assert report.failure_details.error_type == "DirtyRollbackError"
    assert "dirty rollback" in report.reason

    # Verify tool invocation transitioned to OUTCOME_UNKNOWN
    tool_rec = adapter.get_tool_invocation_record(inv_id, ns)
    assert tool_rec is not None
    assert tool_rec.status == ToolCallStatus.OUTCOME_UNKNOWN

    # Verify step was failed
    step = adapter.get_step(step_id, ns)
    assert step is not None
    assert step.failure_details is not None
    assert step.failure_details.error_code == "PARTIAL_DIRTY_ROLLBACK"
    row = adapter._conn.execute(
        "SELECT state FROM run_steps WHERE step_id = ?", (str(step_id),)
    ).fetchone()
    assert row is not None and row["state"] == "failed"


def test_dirty_orphaned_run_reconcile_to_paused() -> None:
    """A dirty run reconciled to PAUSED requires manual intervention (PARTIAL_DIRTY_ROLLBACK)."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns = NamespaceId("ns_recovery")
    run_id = RunId("run_dirty_paused")
    step_id = StepId("step_dirty_paused_1")
    now = datetime.now(UTC)

    _create_test_run(adapter, run_id, ns, status=RunStatus.RUNNING)
    adapter.record_step_start(
        step_id=step_id,
        run_id=run_id,
        namespace_id=ns,
        step_number=1,
        started_at=now,
    )

    inv_id = ToolInvocationId("inv_idemp_write_1")
    adapter.record_tool_intent(
        invocation_id=inv_id,
        step_id=step_id,
        namespace_id=ns,
        tool_name="write_file",
        arguments={"path": "/tmp/out.txt", "data": "abc"},
        side_effect=SideEffectType.IDEMPOTENT_WRITE,
        idempotency_key="idemp_write_1",
        dispatched_at=now,
    )

    report = adapter.reconcile_orphaned_run(run_id, ns, strategy="pause")

    assert report.is_dirty is True
    assert report.new_status == RunStatus.PAUSED
    assert report.failure_details.error_code == "PARTIAL_DIRTY_ROLLBACK"

    updated_run = adapter.get_run(run_id, ns)
    assert updated_run is not None
    assert updated_run.status == RunStatus.PAUSED


def test_read_only_tool_pending_is_not_dirty() -> None:
    """An in-flight read-only tool is transitioned to OUTCOME_UNKNOWN without dirty flag."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns = NamespaceId("ns_recovery")
    run_id = RunId("run_readonly_1")
    step_id = StepId("step_ro_1")
    now = datetime.now(UTC)

    _create_test_run(adapter, run_id, ns, status=RunStatus.RUNNING)
    adapter.record_step_start(
        step_id=step_id,
        run_id=run_id,
        namespace_id=ns,
        step_number=1,
        started_at=now,
    )

    inv_id = ToolInvocationId("inv_ro_1")
    adapter.record_tool_intent(
        invocation_id=inv_id,
        step_id=step_id,
        namespace_id=ns,
        tool_name="read_file",
        arguments={"path": "/etc/hosts"},
        side_effect=SideEffectType.READ_ONLY,
        idempotency_key="idemp_ro_1",
        dispatched_at=now,
    )

    report = adapter.reconcile_orphaned_run(run_id, ns, strategy="fail")

    assert report.is_dirty is False
    assert report.mutating_tool_invocations_count == 0
    assert report.failure_details.error_code == "ORPHANED_RUN_DETECTED"

    tool_rec = adapter.get_tool_invocation_record(inv_id, ns)
    assert tool_rec is not None
    assert tool_rec.status == ToolCallStatus.OUTCOME_UNKNOWN


def test_already_terminal_run_idempotence() -> None:
    """Reconciling an already completed or failed run must be an idempotent no-op."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns = NamespaceId("ns_recovery")
    run_id = RunId("run_already_terminal")
    _create_test_run(adapter, run_id, ns, status=RunStatus.COMPLETED)

    report = adapter.reconcile_orphaned_run(run_id, ns, strategy="fail")
    assert report.previous_status == RunStatus.COMPLETED
    assert report.new_status == RunStatus.COMPLETED
    assert report.is_dirty is False
    assert "Run is already in terminal state" in report.reason


def test_cross_namespace_reconciliation_rejected() -> None:
    """Reconciling a run under a different namespace must raise CrossNamespaceAccessError."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns_owner = NamespaceId("ns_owner")
    ns_attacker = NamespaceId("ns_attacker")
    run_id = RunId("run_ns_test")

    _create_test_run(adapter, run_id, ns_owner, status=RunStatus.RUNNING)

    with pytest.raises(CrossNamespaceAccessError):
        adapter.reconcile_orphaned_run(run_id, ns_attacker, strategy="fail")


def test_nonexistent_run_raises_record_not_found() -> None:
    """Attempting to reconcile a non-existent run ID must raise RecordNotFoundError."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns = NamespaceId("ns_recovery")

    with pytest.raises(RecordNotFoundError):
        adapter.reconcile_orphaned_run(RunId("nonexistent"), ns)


def test_invalid_strategy_raises_value_error() -> None:
    """Invalid recovery strategy strings must be rejected immediately."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns = NamespaceId("ns_recovery")
    run_id = RunId("run_bad_strat")
    _create_test_run(adapter, run_id, ns, status=RunStatus.RUNNING)

    with pytest.raises(ValueError, match="Unknown recovery strategy 'destroy'"):
        adapter.reconcile_orphaned_run(run_id, ns, strategy="destroy")  # type: ignore[arg-type]


def test_batch_reconcile_all_orphaned_runs() -> None:
    """Batch scanning reconciles all orphaned runs while leaving completed runs untouched."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns = NamespaceId("ns_batch")
    now = datetime.now(UTC)

    # Run 1: clean running
    run1 = RunId("run_batch_1")
    _create_test_run(adapter, run1, ns, status=RunStatus.RUNNING)

    # Run 2: dirty running with mutating tool
    run2 = RunId("run_batch_2")
    _create_test_run(adapter, run2, ns, status=RunStatus.RUNNING)
    step2 = StepId("step_b2")
    adapter.record_step_start(
        step_id=step2, run_id=run2, namespace_id=ns, step_number=1, started_at=now
    )
    adapter.record_tool_intent(
        invocation_id=ToolInvocationId("inv_b2"),
        step_id=step2,
        namespace_id=ns,
        tool_name="mutate_db",
        arguments={},
        side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
        idempotency_key="key_b2",
        dispatched_at=now,
    )

    # Run 3: completed
    run3 = RunId("run_batch_3")
    _create_test_run(adapter, run3, ns, status=RunStatus.COMPLETED)

    # Scan and reconcile all
    reports = adapter.reconcile_all_orphaned_runs(ns, strategy="fail")
    assert len(reports) == 2

    report_map = {r.run_id: r for r in reports}
    assert report_map[run1].is_dirty is False
    assert report_map[run1].new_status == RunStatus.FAILED
    assert report_map[run2].is_dirty is True
    assert report_map[run2].new_status == RunStatus.FAILED
    assert report_map[run2].failure_details.error_code == "PARTIAL_DIRTY_ROLLBACK"

    # Completed run untouched
    run3_state = adapter.get_run(run3, ns)
    assert run3_state is not None
    assert run3_state.status == RunStatus.COMPLETED

    # No more orphaned runs
    assert adapter.list_orphaned_runs(ns) == ()


def test_agent_runtime_reconcile_orphaned_runs_integration() -> None:
    """AgentRuntime delegates orphaned run reconciliation to underlying storage."""
    adapter = SQLiteStorageAdapter(":memory:")
    ns = NamespaceId("ns_runtime_rec")
    run_id = RunId("run_rt_orphan")
    _create_test_run(adapter, run_id, ns, status=RunStatus.RUNNING)

    runtime = AgentRuntime(model=FakeModelAdapter(), storage=adapter)
    reports = runtime.reconcile_orphaned_runs(ns, strategy="fail")

    assert len(reports) == 1
    assert reports[0].run_id == run_id
    assert reports[0].new_status == RunStatus.FAILED

    # Without storage configured, returns empty tuple
    runtime_no_storage = AgentRuntime(model=FakeModelAdapter())
    assert runtime_no_storage.reconcile_orphaned_runs(ns) == ()


def test_dirty_rollback_error_attributes() -> None:
    """DirtyRollbackError must have non-recoverable flag and PARTIAL_DIRTY_ROLLBACK error code."""
    err = DirtyRollbackError("Dirty crash occurred")
    assert err.error_code == "PARTIAL_DIRTY_ROLLBACK"
    assert err.recoverable is False
    assert "recovery_hint" in err.details

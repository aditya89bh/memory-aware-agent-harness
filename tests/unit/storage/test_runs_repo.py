"""Unit tests for SQLite runs and steps repository with namespace scoping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memory_aware_agent_harness.contracts import (
    CrossNamespaceAccessError,
    FailureDetails,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepId,
    StepResult,
    TokenUsage,
    ToolCallStatus,
    ToolInvocationId,
    ToolResult,
)
from memory_aware_agent_harness.storage.connection import create_connection
from memory_aware_agent_harness.storage.migrations import apply_migrations
from memory_aware_agent_harness.storage.runs_repo import (
    get_run,
    get_step,
    list_steps_for_run,
    save_run,
    save_step,
    update_run_status,
)


def _make_sample_run(
    run_id: str = "run_01",
    namespace_id: str = "ns_alpha",
    step_id: str = "step_01",
) -> RunResult:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    step = StepResult(
        step_id=StepId(step_id),
        run_id=RunId(run_id),
        step_number=1,
        decision=ModelDecision(
            decision_id="dec_01",
            step_id=StepId("step_01"),
            finish_reason=ModelFinishReason.STOP,
            content="Task completed",
        ),
        tool_results=(),
        memories_proposed=0,
        memories_committed=0,
        tokens_consumed=150,
        completed_at=now,
    )
    tool_res = ToolResult(
        invocation_id=ToolInvocationId("inv_01"),
        tool_name="test_tool",
        status=ToolCallStatus.COMPLETED,
        side_effect=SideEffectType.READ_ONLY,
        idempotency_key="key_01",
        result={"found": True},
    )
    return RunResult(
        run_id=RunId(run_id),
        namespace_id=NamespaceId(namespace_id),
        status=RunStatus.COMPLETED,
        step_count=1,
        total_tokens_consumed=150,
        final_output="Done",
        trace_id="tr_01",
        token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        step_history=(step,),
        tool_results=(tool_res,),
    )


def test_save_and_get_run_roundtrip() -> None:
    """RunResult saves and roundtrips with steps and tool results intact."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)

        run = _make_sample_run()
        save_run(conn, run)

        fetched = get_run(conn, run.run_id, run.namespace_id)
        assert fetched is not None
        assert fetched.run_id == run.run_id
        assert fetched.namespace_id == run.namespace_id
        assert fetched.status == RunStatus.COMPLETED
        assert len(fetched.step_history) == 1
        assert fetched.step_history[0].step_id == StepId("step_01")
        assert len(fetched.tool_results) == 1
        assert fetched.final_output == "Done"

        # Missing run returns None
        assert get_run(conn, RunId("missing_run"), run.namespace_id) is None
    finally:
        conn.close()


def test_run_cross_namespace_access_denied() -> None:
    """Accessing or mutating a run under a different namespace raises CrossNamespaceAccessError."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)

        run = _make_sample_run(run_id="run_alpha", namespace_id="ns_alpha")
        save_run(conn, run)

        # Cross-namespace read returns None without disclosing existence
        assert get_run(conn, run.run_id, NamespaceId("ns_beta")) is None

        # Cross-namespace write attempt is denied without target disclosure
        tampered_run = _make_sample_run(run_id="run_alpha", namespace_id="ns_beta")
        with pytest.raises(CrossNamespaceAccessError) as exc_info:
            save_run(conn, tampered_run)
        assert exc_info.value.requested_namespace == "ns_beta"
        assert exc_info.value.target_namespace is None
    finally:
        conn.close()


def test_save_and_get_step_roundtrip() -> None:
    """save_step and get_step roundtrip cleanly with namespace verification."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run = _make_sample_run(run_id="run_step_test", namespace_id="ns_alpha")
        save_run(conn, run)

        now = datetime(2026, 10, 14, 12, 5, 0, tzinfo=UTC)
        step2 = StepResult(
            step_id=StepId("step_02"),
            run_id=run.run_id,
            step_number=2,
            decision=ModelDecision(
                decision_id="dec_02",
                step_id=StepId("step_02"),
                finish_reason=ModelFinishReason.STOP,
                content="Step 2 done",
            ),
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
            tokens_consumed=50,
            completed_at=now,
        )
        save_step(conn, step2, run.namespace_id)

        fetched_step = get_step(conn, step2.step_id, run.namespace_id)
        assert fetched_step is not None
        assert fetched_step.step_id == StepId("step_02")
        assert fetched_step.step_number == 2

        all_steps = list_steps_for_run(conn, run.run_id, run.namespace_id)
        assert len(all_steps) == 2
        assert [s.step_number for s in all_steps] == [1, 2]

        # Missing step returns None
        assert get_step(conn, StepId("missing_step"), run.namespace_id) is None
    finally:
        conn.close()


def test_step_cross_namespace_access_denied() -> None:
    """Step operations validate parent run existence and enforce namespace isolation."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run = _make_sample_run(run_id="run_01", namespace_id="ns_alpha")
        save_run(conn, run)

        now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
        orphan_step = StepResult(
            step_id=StepId("orphan"),
            run_id=RunId("nonexistent_run"),
            step_number=1,
            decision=ModelDecision(
                decision_id="dec_orphan",
                step_id=StepId("orphan"),
                finish_reason=ModelFinishReason.STOP,
            ),
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
            tokens_consumed=0,
            completed_at=now,
        )

        # Saving step to nonexistent run raises RecordNotFoundError
        with pytest.raises(RecordNotFoundError):
            save_step(conn, orphan_step, NamespaceId("ns_alpha"))

        # Saving step where parent run belongs to another namespace raises CrossNamespaceAccessError
        step_for_run = StepResult(
            step_id=StepId("step_wrong_ns"),
            run_id=run.run_id,
            step_number=2,
            decision=ModelDecision(
                decision_id="dec_w",
                step_id=StepId("step_wrong_ns"),
                finish_reason=ModelFinishReason.STOP,
            ),
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
            tokens_consumed=0,
            completed_at=now,
        )
        with pytest.raises(CrossNamespaceAccessError):
            save_step(conn, step_for_run, NamespaceId("ns_attacker"))

        # Overwriting existing step with forged step ID in another namespace
        # raises CrossNamespaceAccessError
        run_beta = _make_sample_run(
            run_id="run_beta",
            namespace_id="ns_beta",
            step_id="step_beta_01",
        )
        save_run(conn, run_beta)
        colliding_step = StepResult(
            step_id=StepId("step_01"),  # belongs to run_01 / ns_alpha
            run_id=run_beta.run_id,
            step_number=2,
            decision=ModelDecision(
                decision_id="dec_c",
                step_id=StepId("step_01"),
                finish_reason=ModelFinishReason.STOP,
            ),
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
            tokens_consumed=0,
            completed_at=now,
        )
        with pytest.raises(CrossNamespaceAccessError):
            save_step(conn, colliding_step, NamespaceId("ns_beta"))

        # Updating step in place in same namespace works
        updated_step = StepResult(
            step_id=StepId("step_01"),
            run_id=run.run_id,
            step_number=1,
            decision=ModelDecision(
                decision_id="dec_01_updated",
                step_id=StepId("step_01"),
                finish_reason=ModelFinishReason.STOP,
                content="Updated content",
            ),
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
            tokens_consumed=150,
            completed_at=now,
        )
        save_step(conn, updated_step, run.namespace_id, state="modified")
        fetched_updated = get_step(conn, StepId("step_01"), run.namespace_id)
        assert fetched_updated is not None
        assert fetched_updated.decision is not None
        assert fetched_updated.decision.content == "Updated content"

        # Reading step under wrong namespace returns None without disclosing existence
        assert get_step(conn, StepId("step_01"), NamespaceId("ns_attacker")) is None

        # Listing steps under wrong namespace returns empty tuple without disclosing existence
        assert list_steps_for_run(conn, run.run_id, NamespaceId("ns_attacker")) == ()

        # Listing steps for nonexistent run returns empty tuple
        assert list_steps_for_run(conn, RunId("missing_run"), NamespaceId("ns_alpha")) == ()
    finally:
        conn.close()


def test_update_run_status() -> None:
    """update_run_status changes lifecycle status with namespace enforcement."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run = _make_sample_run(run_id="run_status_test", namespace_id="ns_alpha")
        save_run(conn, run)

        completed_dt = datetime(2026, 10, 14, 13, 0, 0, tzinfo=UTC)
        update_run_status(
            conn,
            run.run_id,
            run.namespace_id,
            RunStatus.FAILED,
            error="Custom failure occurred",
            completed_at=completed_dt,
        )

        fetched = get_run(conn, run.run_id, run.namespace_id)
        assert fetched is not None
        assert fetched.status == RunStatus.FAILED
        assert fetched.error == "Custom failure occurred"
        assert fetched.failure_details is not None

        # Test default error message generation on ABORTED
        update_run_status(
            conn,
            run.run_id,
            run.namespace_id,
            RunStatus.ABORTED,
        )
        fetched_aborted = get_run(conn, run.run_id, run.namespace_id)
        assert fetched_aborted is not None
        assert fetched_aborted.status == RunStatus.ABORTED
        assert fetched_aborted.error == "Run execution aborted"

        # Test updating to COMPLETED clears error and failure_details
        update_run_status(
            conn,
            run.run_id,
            run.namespace_id,
            RunStatus.COMPLETED,
        )
        fetched_completed = get_run(conn, run.run_id, run.namespace_id)
        assert fetched_completed is not None
        assert fetched_completed.status == RunStatus.COMPLETED
        assert fetched_completed.error is None
        assert fetched_completed.failure_details is None

        # Test updating to CANCELLED with custom error and failure details
        fd = FailureDetails(
            error_type="CancelError",
            message="User cancelled",
            error_code="USER_CANCEL",
        )
        update_run_status(
            conn,
            run.run_id,
            run.namespace_id,
            RunStatus.CANCELLED,
            error="User requested cancellation",
            failure_details=fd,
        )
        fetched_cancelled = get_run(conn, run.run_id, run.namespace_id)
        assert fetched_cancelled is not None
        assert fetched_cancelled.status == RunStatus.CANCELLED
        assert fetched_cancelled.error == "User requested cancellation"
        assert fetched_cancelled.failure_details is not None
        assert fetched_cancelled.failure_details.error_type == "CancelError"

        # Nonexistent run raises RecordNotFoundError
        with pytest.raises(RecordNotFoundError):
            update_run_status(
                conn,
                RunId("missing"),
                NamespaceId("ns_alpha"),
                RunStatus.CANCELLED,
            )

        # Wrong namespace raises CrossNamespaceAccessError
        with pytest.raises(CrossNamespaceAccessError):
            update_run_status(
                conn,
                run.run_id,
                NamespaceId("ns_attacker"),
                RunStatus.CANCELLED,
            )
    finally:
        conn.close()

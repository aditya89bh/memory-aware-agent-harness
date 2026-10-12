"""Unit tests for execution contexts, protocol requirements, and structured failures."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    BudgetExceededError,
    CancellationToken,
    CompensationFailedError,
    ContextItem,
    ContextManifest,
    DeadlineExceededError,
    ExecutionCancelledError,
    FailureDetails,
    FrozenDict,
    HarnessError,
    InvalidStateTransitionError,
    MemoryId,
    MemoryKind,
    ModelCallContext,
    ModelDecision,
    ModelFinishReason,
    ModelPort,
    NamespaceId,
    PolicyViolationError,
    ReplayDivergenceError,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepId,
    StepResult,
    StorageError,
    TokenUsage,
    ToolCallContext,
    ToolCallRequest,
    ToolCallStatus,
    ToolExecutionError,
    ToolInvocationId,
    ToolProtocol,
    ToolResult,
)


class DummyModel:
    """Mock implementation of ModelPort."""

    def decide(
        self,
        manifest: ContextManifest,
        tools_schema: Sequence[Mapping[str, Any]],
        context: ModelCallContext,
    ) -> ModelDecision:
        context.check_cancellation()
        return ModelDecision(
            decision_id="dec_01",
            step_id=context.step_id,
            finish_reason=ModelFinishReason.STOP,
            content="test decision",
        )


class DummyTool:
    """Mock implementation of ToolProtocol."""

    name = "mock_tool"
    description = "A mock tool for testing protocol enforcement."
    side_effect = SideEffectType.READ_ONLY

    def get_schema(self) -> Mapping[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolCallContext,
    ) -> Mapping[str, Any]:
        context.check_cancellation()
        return {"echo": arguments, "invoked_by": str(context.invocation_id)}


def test_model_port_decide_requires_model_call_context() -> None:
    """ModelPort.decide requires ModelCallContext with no default value."""
    sig = inspect.signature(ModelPort.decide)
    params = list(sig.parameters.values())
    assert len(params) == 4  # self, manifest, tools_schema, context
    ctx_param = params[3]
    assert ctx_param.name == "context"
    assert ctx_param.default is inspect.Parameter.empty
    assert ctx_param.annotation in (ModelCallContext, "ModelCallContext")

    dummy = DummyModel()
    assert isinstance(dummy, ModelPort)

    manifest = ContextManifest(
        manifest_id="mf_01",
        step_id=StepId("step_01"),
        included_items=(
            ContextItem(
                memory_id=MemoryId("mem_01"),
                kind=MemoryKind.WORKING,
                content="working content",
                rank_score=0.9,
                provenance_id="prov_01",
                token_cost=10,
            ),
        ),
        excluded_item_ids={},
        total_tokens=10,
        budget_limit=1000,
        compiled_prompt="sys prompt",
    )

    valid_ctx = ModelCallContext(
        run_id=RunId("run_01"),
        step_id=StepId("step_01"),
        namespace_id=NamespaceId("ns_01"),
        budget_remaining_tokens=500,
    )

    # Calling with required context succeeds
    decision = dummy.decide(manifest, [], valid_ctx)
    assert decision.content == "test decision"

    # Calling without context raises TypeError
    with pytest.raises(TypeError):
        dummy.decide(manifest, [])  # type: ignore[call-arg]


def test_tool_protocol_execute_requires_tool_call_context() -> None:
    """ToolProtocol.execute requires ToolCallContext with no default value."""
    sig = inspect.signature(ToolProtocol.execute)
    params = list(sig.parameters.values())
    assert len(params) == 3  # self, arguments, context
    ctx_param = params[2]
    assert ctx_param.name == "context"
    assert ctx_param.default is inspect.Parameter.empty
    assert ctx_param.annotation in (ToolCallContext, "ToolCallContext")

    dummy = DummyTool()
    assert isinstance(dummy, ToolProtocol)

    valid_ctx = ToolCallContext(
        run_id=RunId("run_01"),
        step_id=StepId("step_01"),
        namespace_id=NamespaceId("ns_01"),
        invocation_id=ToolInvocationId("inv_01"),
        idempotency_key="idemp_01",
    )

    # Calling with required context succeeds
    obs = dummy.execute({"arg": 1}, valid_ctx)
    assert obs["invoked_by"] == "inv_01"

    # Calling without context raises TypeError
    with pytest.raises(TypeError):
        dummy.execute({"arg": 1})  # type: ignore[call-arg]


def test_check_cancellation_semantics() -> None:
    """check_cancellation raises ExecutionCancelledError when is_cancelled is True."""
    # ModelCallContext
    active_mctx = ModelCallContext(
        run_id=RunId("run_01"),
        step_id=StepId("step_01"),
        namespace_id=NamespaceId("ns_01"),
        budget_remaining_tokens=100,
        is_cancelled=False,
    )
    active_mctx.check_cancellation()  # does not raise

    cancelled_mctx = ModelCallContext(
        run_id=RunId("run_01"),
        step_id=StepId("step_01"),
        namespace_id=NamespaceId("ns_01"),
        budget_remaining_tokens=100,
        is_cancelled=True,
    )
    with pytest.raises(ExecutionCancelledError) as exc_info:
        cancelled_mctx.check_cancellation()
    assert exc_info.value.error_code == "EXECUTION_CANCELLED"
    assert exc_info.value.details["run_id"] == "run_01"
    assert exc_info.value.details["step_id"] == "step_01"
    assert exc_info.value.recoverable is False

    # ToolCallContext
    active_tctx = ToolCallContext(
        run_id=RunId("run_01"),
        step_id=StepId("step_01"),
        namespace_id=NamespaceId("ns_01"),
        invocation_id=ToolInvocationId("inv_01"),
        idempotency_key="idemp_01",
        is_cancelled=False,
    )
    active_tctx.check_cancellation()  # does not raise

    cancelled_tctx = ToolCallContext(
        run_id=RunId("run_01"),
        step_id=StepId("step_01"),
        namespace_id=NamespaceId("ns_01"),
        invocation_id=ToolInvocationId("inv_01"),
        idempotency_key="idemp_01",
        is_cancelled=True,
    )
    with pytest.raises(ExecutionCancelledError) as t_exc_info:
        cancelled_tctx.check_cancellation()
    assert t_exc_info.value.error_code == "EXECUTION_CANCELLED"
    assert t_exc_info.value.details["invocation_id"] == "inv_01"


def test_deadline_and_expiration_semantics() -> None:
    """check_deadline raises DeadlineExceededError when deadline has passed."""
    t0 = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    deadline = t0 + timedelta(seconds=30)

    mctx = ModelCallContext(
        run_id=RunId("run_01"),
        step_id=StepId("step_01"),
        namespace_id=NamespaceId("ns_01"),
        budget_remaining_tokens=100,
        deadline=deadline,
    )

    # Before deadline
    assert mctx.is_expired(t0) is False
    mctx.check_deadline(t0)  # does not raise

    # At deadline
    assert mctx.is_expired(deadline) is True
    with pytest.raises(DeadlineExceededError) as exc_info:
        mctx.check_deadline(deadline)
    assert exc_info.value.error_code == "DEADLINE_EXCEEDED"
    assert exc_info.value.deadline == deadline

    # After deadline
    t_after = deadline + timedelta(seconds=5)
    assert mctx.is_expired(t_after) is True
    with pytest.raises(DeadlineExceededError):
        mctx.check_deadline(t_after)

    # Rejection of naive datetime in now
    with pytest.raises(ValueError, match=r"now must be timezone-aware"):
        mctx.is_expired(datetime(2026, 10, 12, 10, 0, 0))

    # ToolCallContext deadline
    tctx = ToolCallContext(
        run_id=RunId("run_01"),
        step_id=StepId("step_01"),
        namespace_id=NamespaceId("ns_01"),
        invocation_id=ToolInvocationId("inv_01"),
        idempotency_key="idemp_01",
        deadline=deadline,
    )
    assert tctx.is_expired(t0) is False
    tctx.check_deadline(t0)

    assert tctx.is_expired(t_after) is True
    with pytest.raises(DeadlineExceededError) as t_exc_info:
        tctx.check_deadline(t_after)
    assert t_exc_info.value.error_code == "DEADLINE_EXCEEDED"
    assert t_exc_info.value.details["invocation_id"] == "inv_01"


def test_token_usage_and_failure_details_integration_roundtrip() -> None:
    """TokenUsage and FailureDetails integrate into decisions, tool results, steps, and runs."""
    usage = TokenUsage(prompt_tokens=120, completion_tokens=30, total_tokens=150)
    failure = FailureDetails(
        error_type="SandboxTimeoutError",
        message="Sandbox execution timed out after 30s",
        step_id=StepId("step_01"),
        traceback_summary="TimeoutError in sandbox.py:42",
        recovery_hint="Retry with larger timeout",
        error_code="SANDBOX_TIMEOUT",
    )

    # ModelDecision integration
    decision = ModelDecision(
        decision_id="dec_01",
        step_id=StepId("step_01"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        content="calling tool",
        tool_calls=(ToolCallRequest(call_id="call_1", tool_name="search", arguments={"q": "rag"}),),
        tokens_used=150,
        token_usage=usage,
        failure_details=None,
    )
    d_dict = decision.to_dict()
    assert d_dict["token_usage"]["total_tokens"] == 150
    decision_restored = ModelDecision.from_dict(d_dict)
    assert decision_restored.token_usage == usage
    assert decision_restored.to_json() == decision.to_json()

    # ToolResult integration
    tool_res = ToolResult(
        invocation_id=ToolInvocationId("inv_01"),
        tool_name="search",
        status=ToolCallStatus.FAILED,
        side_effect=SideEffectType.READ_ONLY,
        idempotency_key="idemp_01",
        failure_details=failure,
    )
    assert tool_res.error == failure.message
    tr_dict = tool_res.to_dict()
    assert tr_dict["failure_details"]["error_code"] == "SANDBOX_TIMEOUT"
    tool_res_restored = ToolResult.from_dict(tr_dict)
    assert tool_res_restored.failure_details == failure
    assert tool_res_restored.to_json() == tool_res.to_json()

    # StepResult integration
    step_res = StepResult(
        step_id=StepId("step_01"),
        run_id=RunId("run_01"),
        step_number=1,
        decision=decision,
        tool_results=(tool_res,),
        memories_proposed=2,
        memories_committed=2,
        tokens_consumed=0,  # will be auto-derived from token_usage
        completed_at=datetime(2026, 10, 12, 10, 0, 5, tzinfo=UTC),
        token_usage=usage,
        failure_details=failure,
    )
    assert step_res.tokens_consumed == 150
    sr_dict = step_res.to_dict()
    assert sr_dict["token_usage"]["prompt_tokens"] == 120
    assert sr_dict["failure_details"]["error_type"] == "SandboxTimeoutError"
    step_res_restored = StepResult.from_dict(sr_dict)
    assert step_res_restored.token_usage == usage
    assert step_res_restored.failure_details == failure

    # RunResult integration
    run_res = RunResult(
        run_id=RunId("run_01"),
        namespace_id=NamespaceId("ns_01"),
        status=RunStatus.FAILED,
        step_count=1,
        total_tokens_consumed=0,  # will be auto-derived from token_usage
        final_output=None,
        trace_id="trace_01",
        token_usage=usage,
        failure_details=failure,
    )
    assert run_res.total_tokens_consumed == 150
    assert run_res.error == failure.message
    rr_dict = run_res.to_dict()
    assert rr_dict["failure_details"]["error_code"] == "SANDBOX_TIMEOUT"
    run_res_restored = RunResult.from_dict(rr_dict)
    assert run_res_restored.token_usage == usage
    assert run_res_restored.failure_details == failure


def test_structured_exceptions_expose_code_details_and_recoverable() -> None:
    """All HarnessError subclasses expose error_code, details, and recoverable."""
    exceptions = [
        HarnessError("Generic error", error_code="CUSTOM_GENERIC", recoverable=True),
        BudgetExceededError("Tokens exhausted", budget_type="tokens", limit=1000, observed=1200),
        PolicyViolationError("Forbidden tool call", policy_name="sandbox_network_policy"),
        ToolExecutionError(
            "Tool crashed",
            tool_name="stripe",
            invocation_id=ToolInvocationId("inv_9"),
            recoverable=True,
        ),
        CompensationFailedError("Undo failed", step_id=StepId("step_5")),
        ReplayDivergenceError(
            "State diverged",
            expected_hash="abc",
            actual_hash="xyz",
        ),
        StorageError("Disk full", operation="sqlite_insert", recoverable=False),
        ExecutionCancelledError("Run aborted by user"),
        DeadlineExceededError(
            "Execution timeout",
            deadline=datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC),
        ),
        InvalidStateTransitionError(
            "Illegal FSM jump",
            from_state="completed",
            to_state="running",
        ),
    ]

    for exc in exceptions:
        assert isinstance(exc.error_code, str) and exc.error_code
        assert isinstance(exc.details, (Mapping, FrozenDict))
        assert isinstance(exc.recoverable, bool)

        # FailureDetails conversion works
        fd = exc.to_failure_details(step_id=StepId("step_01"))
        assert isinstance(fd, FailureDetails)
        assert fd.error_type == type(exc).__name__
        assert fd.error_code == exc.error_code
        assert fd.message == exc.message
        assert fd.step_id == StepId("step_01")


def test_r3_live_cancellation_token_signaling() -> None:
    """R-3 regression test: CancellationToken supports live signaling, callbacks, and checks."""
    token = CancellationToken()
    assert bool(token.is_cancelled) is False
    assert (token.reason is None) is True

    callback_invocations: list[str] = []

    def on_cancel() -> None:
        callback_invocations.append("cancelled")

    token.add_callback(on_cancel)
    assert not callback_invocations

    ctx = ModelCallContext(
        run_id=RunId("run_live_01"),
        step_id=StepId("step_live_01"),
        namespace_id=NamespaceId("ns_01"),
        budget_remaining_tokens=5000,
        cancellation_token=token,
    )

    # Initial check passes
    ctx.check_cancellation()

    # Cancel live
    token.cancel(reason="User clicked Stop")
    assert token.is_cancelled
    assert token.reason == "User clicked Stop"
    assert len(callback_invocations) == 1

    # check_cancellation now raises ExecutionCancelledError
    with pytest.raises(ExecutionCancelledError) as exc_info:
        ctx.check_cancellation()

    assert "User clicked Stop" in str(exc_info.value)
    assert exc_info.value.details["cancellation_reason"] == "User clicked Stop"


def test_r3_live_cancellation_during_in_progress_model_call() -> None:
    """R-3 regression test: in-progress model call cooperatively aborts upon cancellation."""
    import threading
    import time

    token = CancellationToken()
    ctx = ModelCallContext(
        run_id=RunId("run_stream"),
        step_id=StepId("step_stream"),
        namespace_id=NamespaceId("ns_01"),
        budget_remaining_tokens=10000,
        cancellation_token=token,
    )

    iterations_completed = 0
    aborted_with_cancel = False

    def simulated_model_worker() -> None:
        nonlocal iterations_completed, aborted_with_cancel
        try:
            for _ in range(50):
                ctx.check_cancellation()
                time.sleep(0.01)
                iterations_completed += 1
        except ExecutionCancelledError:
            aborted_with_cancel = True

    worker = threading.Thread(target=simulated_model_worker)
    worker.start()

    # Allow worker to run a few iterations, then cancel
    time.sleep(0.03)
    token.cancel(reason="Live timeout threshold exceeded")
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert aborted_with_cancel
    assert iterations_completed < 50


def test_r3_cancellation_token_serialization_roundtrip() -> None:
    """R-3 regression test: ModelCallContext with CancellationToken round-trips correctly."""
    token = CancellationToken(cancelled=True, reason="Pre-cancelled task")
    ctx = ModelCallContext(
        run_id=RunId("run_ser"),
        step_id=StepId("step_ser"),
        namespace_id=NamespaceId("ns_01"),
        budget_remaining_tokens=2500,
        cancellation_token=token,
    )

    d = ctx.to_dict()
    assert d["cancellation_token"] == {"is_cancelled": True, "reason": "Pre-cancelled task"}

    restored = ModelCallContext.from_dict(d)
    assert restored.cancellation_token is not None
    assert restored.cancellation_token.is_cancelled
    assert restored.cancellation_token.reason == "Pre-cancelled task"
    with pytest.raises(ExecutionCancelledError):
        restored.check_cancellation()

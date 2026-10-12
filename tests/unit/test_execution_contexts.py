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
    RunBudgets,
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


def test_r4_token_usage_arithmetic_and_monotonicity() -> None:
    """R-4 regression test: TokenUsage supports zero factory, addition, and monotonic checks."""
    z = TokenUsage.zero()
    assert z.prompt_tokens == 0
    assert z.completion_tokens == 0
    assert z.total_tokens == 0

    u1 = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    u2 = TokenUsage(prompt_tokens=200, completion_tokens=100, total_tokens=300)

    u_sum = u1 + u2
    assert u_sum.prompt_tokens == 300
    assert u_sum.completion_tokens == 150
    assert u_sum.total_tokens == 450

    # Monotonic checks
    assert u_sum.is_monotonically_ge(u1)
    assert u_sum.is_monotonically_ge(u2)
    assert not u1.is_monotonically_ge(u_sum)

    # Incompatible addition returns NotImplemented
    assert u1.__add__("not a token usage") is NotImplemented


def test_r4_run_budgets_checks_and_remainders() -> None:
    """R-4 regression test: RunBudgets provides step, token, time checks and remainder helpers."""
    budgets = RunBudgets(max_steps=10, max_tokens=1000, max_time_seconds=60.0)

    # Step budget checks
    budgets.check_step_budget(9)
    assert budgets.remaining_steps(9) == 1
    assert budgets.remaining_steps(15) == 0
    with pytest.raises(BudgetExceededError) as exc_step:
        budgets.check_step_budget(10)
    assert exc_step.value.budget_type == "steps"
    assert exc_step.value.limit == 10
    assert exc_step.value.observed == 10

    # Token budget checks
    budgets.check_token_budget(1000)
    assert budgets.remaining_tokens(800) == 200
    assert budgets.remaining_tokens(1200) == 0
    with pytest.raises(BudgetExceededError) as exc_tok:
        budgets.check_token_budget(1001)
    assert exc_tok.value.budget_type == "tokens"
    assert exc_tok.value.limit == 1000
    assert exc_tok.value.observed == 1001

    # Time budget checks
    budgets.check_time_budget(59.9)
    with pytest.raises(BudgetExceededError) as exc_time:
        budgets.check_time_budget(60.1)
    assert exc_time.value.budget_type == "time"
    assert exc_time.value.limit == 60.0


def test_r4_model_decision_error_failure_invariants() -> None:
    """R-4 regression test: ModelDecision with finish_reason ERROR requires failure details."""
    with pytest.raises(ValueError, match="ModelDecision with finish_reason ERROR"):
        ModelDecision(
            decision_id="dec_err_01",
            step_id=StepId("step_01"),
            finish_reason=ModelFinishReason.ERROR,
            content=None,
            failure_details=None,
        )

    # Auto-constructs from content if content provided
    dec_from_content = ModelDecision(
        decision_id="dec_err_02",
        step_id=StepId("step_01"),
        finish_reason=ModelFinishReason.ERROR,
        content="Rate limit reached on provider",
    )
    assert dec_from_content.failure_details is not None
    assert dec_from_content.failure_details.message == "Rate limit reached on provider"
    assert dec_from_content.failure_details.error_code == "MODEL_ERROR"


def test_r4_tool_result_failure_and_success_invariants() -> None:
    """R-4 regression test: ToolResult enforces failure details and clean success."""
    # FAILED status without error or failure details raises
    with pytest.raises(ValueError, match="ToolResult with status failed must provide"):
        ToolResult(
            invocation_id=ToolInvocationId("inv_fail_01"),
            tool_name="web_search",
            status=ToolCallStatus.FAILED,
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key="ik_fail_01",
            error=None,
            failure_details=None,
        )

    # OUTCOME_UNKNOWN status without error or failure details raises
    with pytest.raises(ValueError, match="ToolResult with status outcome_unknown must provide"):
        ToolResult(
            invocation_id=ToolInvocationId("inv_unk_01"),
            tool_name="stripe_charge",
            status=ToolCallStatus.OUTCOME_UNKNOWN,
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key="ik_unk_01",
        )

    # COMPLETED status with error raises
    with pytest.raises(ValueError, match="ToolResult with status COMPLETED must not have error"):
        ToolResult(
            invocation_id=ToolInvocationId("inv_comp_01"),
            tool_name="read_file",
            status=ToolCallStatus.COMPLETED,
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key="ik_comp_01",
            error="Unexpected error on completed tool",
        )

    # COMPLETED status with failure_details raises
    with pytest.raises(
        ValueError, match="ToolResult with status COMPLETED must not have failure_details"
    ):
        ToolResult(
            invocation_id=ToolInvocationId("inv_comp_02"),
            tool_name="read_file",
            status=ToolCallStatus.COMPLETED,
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key="ik_comp_02",
            failure_details=FailureDetails(error_type="Error", message="fail", error_code="FAIL"),
        )


def test_r4_step_result_and_run_result_token_and_failure_invariants() -> None:
    """R-4 regression test: StepResult & RunResult enforce token matching & failure rules."""
    now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
    usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    dec = ModelDecision(
        decision_id="dec_01",
        step_id=StepId("step_01"),
        finish_reason=ModelFinishReason.STOP,
        token_usage=usage,
    )

    # StepResult token mismatch raises
    with pytest.raises(ValueError, match=r"StepResult\.tokens_consumed .* does not match"):
        StepResult(
            step_id=StepId("step_01"),
            run_id=RunId("run_01"),
            step_number=1,
            decision=dec,
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
            tokens_consumed=200,  # mismatch with usage.total_tokens=150
            completed_at=now,
            token_usage=usage,
        )

    # StepResult inherits token_usage from decision if omitted
    sr = StepResult(
        step_id=StepId("step_01"),
        run_id=RunId("run_01"),
        step_number=1,
        decision=dec,
        tool_results=(),
        memories_proposed=0,
        memories_committed=0,
        tokens_consumed=0,  # auto-derived
        completed_at=now,
    )
    assert sr.tokens_consumed == 150
    assert sr.token_usage == usage

    # RunResult token mismatch raises
    with pytest.raises(ValueError, match=r"RunResult\.total_tokens_consumed .* does not match"):
        RunResult(
            run_id=RunId("run_01"),
            namespace_id=NamespaceId("ns_01"),
            status=RunStatus.COMPLETED,
            step_count=1,
            total_tokens_consumed=999,  # mismatch with usage.total_tokens=150
            final_output="Done",
            trace_id="tr_01",
            token_usage=usage,
        )

    # RunResult FAILED without error or failure_details raises
    with pytest.raises(ValueError, match="RunResult with status failed must provide"):
        RunResult(
            run_id=RunId("run_01"),
            namespace_id=NamespaceId("ns_01"),
            status=RunStatus.FAILED,
            step_count=1,
            total_tokens_consumed=150,
            final_output=None,
            trace_id="tr_01",
            error=None,
            failure_details=None,
        )

    # RunResult COMPLETED with error raises
    with pytest.raises(ValueError, match="RunResult with status COMPLETED must not have error"):
        RunResult(
            run_id=RunId("run_01"),
            namespace_id=NamespaceId("ns_01"),
            status=RunStatus.COMPLETED,
            step_count=1,
            total_tokens_consumed=150,
            final_output="Done",
            trace_id="tr_01",
            error="Should not be here",
        )

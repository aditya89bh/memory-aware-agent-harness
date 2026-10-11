"""Tests for Run and Step FSM legal transitions, execution contexts, and deterministic ports."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    LEGAL_RUN_TRANSITIONS,
    LEGAL_STEP_TRANSITIONS,
    ClockPort,
    FailureDetails,
    IdGeneratorPort,
    ModelCallContext,
    NamespaceId,
    RunBudgets,
    RunId,
    RunStatus,
    StepId,
    StepState,
    TokenUsage,
    ToolCallContext,
    ToolInvocationId,
)


def _attempt_setitem(target: Any, key: Any, val: Any) -> None:
    target[key] = val


def test_legal_run_transitions_matrix() -> None:
    """Run transitions strictly enforce permitted lifecycle trajectories."""
    # Permitted transitions from INITIALIZED
    assert RunStatus.RUNNING in LEGAL_RUN_TRANSITIONS[RunStatus.INITIALIZED]
    assert RunStatus.CANCELLED in LEGAL_RUN_TRANSITIONS[RunStatus.INITIALIZED]
    assert RunStatus.FAILED in LEGAL_RUN_TRANSITIONS[RunStatus.INITIALIZED]
    assert RunStatus.COMPLETED not in LEGAL_RUN_TRANSITIONS[RunStatus.INITIALIZED]

    # Permitted transitions from RUNNING
    running_targets = LEGAL_RUN_TRANSITIONS[RunStatus.RUNNING]
    assert RunStatus.PAUSED in running_targets
    assert RunStatus.COMPLETED in running_targets
    assert RunStatus.BUDGET_EXHAUSTED in running_targets
    assert RunStatus.CANCELLED in running_targets
    assert RunStatus.ABORTED in running_targets
    assert RunStatus.FAILED in running_targets

    # Terminal states have no legal outgoing transitions
    terminal_statuses = [
        RunStatus.COMPLETED,
        RunStatus.BUDGET_EXHAUSTED,
        RunStatus.CANCELLED,
        RunStatus.ABORTED,
        RunStatus.FAILED,
    ]
    for status in terminal_statuses:
        assert len(LEGAL_RUN_TRANSITIONS[status]) == 0

    # Matrix itself is immutable
    with pytest.raises(TypeError):
        _attempt_setitem(LEGAL_RUN_TRANSITIONS, RunStatus.COMPLETED, frozenset({RunStatus.RUNNING}))


def test_legal_step_transitions_matrix() -> None:
    """Step transitions enforce the correct operational pipeline order."""
    # Forward pipeline
    assert StepState.RETRIEVING in LEGAL_STEP_TRANSITIONS[StepState.NOT_STARTED]
    assert StepState.COMPILING_CONTEXT in LEGAL_STEP_TRANSITIONS[StepState.RETRIEVING]
    assert StepState.DECIDING in LEGAL_STEP_TRANSITIONS[StepState.COMPILING_CONTEXT]
    assert StepState.DISPATCHING_TOOL in LEGAL_STEP_TRANSITIONS[StepState.DECIDING]
    assert StepState.EXTRACTING_MEMORIES in LEGAL_STEP_TRANSITIONS[StepState.DISPATCHING_TOOL]
    assert StepState.COMMITTING_MEMORIES in LEGAL_STEP_TRANSITIONS[StepState.EXTRACTING_MEMORIES]
    assert StepState.CHECKPOINTING in LEGAL_STEP_TRANSITIONS[StepState.COMMITTING_MEMORIES]
    assert StepState.COMPLETED in LEGAL_STEP_TRANSITIONS[StepState.CHECKPOINTING]

    # Terminal states
    assert len(LEGAL_STEP_TRANSITIONS[StepState.COMPLETED]) == 0
    assert len(LEGAL_STEP_TRANSITIONS[StepState.FAILED]) == 0
    assert len(LEGAL_STEP_TRANSITIONS[StepState.ABORTED]) == 0

    # Illegal backward leaps
    assert StepState.RETRIEVING not in LEGAL_STEP_TRANSITIONS[StepState.CHECKPOINTING]
    assert StepState.NOT_STARTED not in LEGAL_STEP_TRANSITIONS[StepState.COMPLETED]

    # Immutability
    with pytest.raises(TypeError):
        _attempt_setitem(
            LEGAL_STEP_TRANSITIONS, StepState.COMPLETED, frozenset({StepState.NOT_STARTED})
        )


def test_run_budgets_validation_and_serialization() -> None:
    """RunBudgets enforces positive limits and roundtrips via JSON."""
    budgets = RunBudgets(max_steps=20, max_tokens=50_000, max_time_seconds=120.0)
    assert budgets.max_steps == 20
    assert budgets.max_tokens == 50_000
    assert budgets.max_time_seconds == 120.0

    # Roundtrip
    assert RunBudgets.from_json(budgets.to_json()) == budgets

    # Zero or negative limits rejected
    with pytest.raises(ValueError, match=r"max_steps must be > 0"):
        RunBudgets(max_steps=0)

    with pytest.raises(ValueError, match=r"max_tokens must be > 0"):
        RunBudgets(max_tokens=-10)

    with pytest.raises(ValueError, match=r"max_time_seconds must be > 0\.0"):
        RunBudgets(max_time_seconds=0.0)


def test_token_usage_validation_and_serialization() -> None:
    """TokenUsage validates arithmetic consistency and non-negativity."""
    usage = TokenUsage(prompt_tokens=150, completion_tokens=50, total_tokens=200)
    assert TokenUsage.from_json(usage.to_json()) == usage

    # Negative counts rejected
    with pytest.raises(ValueError, match=r"Token counts must be non-negative"):
        TokenUsage(prompt_tokens=-1, completion_tokens=10, total_tokens=9)

    # Arithmetic mismatch rejected
    pattern = r"total_tokens .* must equal prompt_tokens .* \+ completion_tokens"
    with pytest.raises(ValueError, match=pattern):
        TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=140)


def test_model_and_tool_call_contexts() -> None:
    """ModelCallContext and ToolCallContext validate fields and roundtrip."""
    m_ctx = ModelCallContext(
        run_id=RunId("run_abc"),
        step_id=StepId("step_01"),
        namespace_id=NamespaceId("tenant_x"),
        budget_remaining_tokens=5000,
    )
    assert ModelCallContext.from_json(m_ctx.to_json()) == m_ctx

    with pytest.raises(ValueError, match=r"budget_remaining_tokens must be >= 0"):
        ModelCallContext(
            run_id=RunId("run_abc"),
            step_id=StepId("step_01"),
            namespace_id=NamespaceId("tenant_x"),
            budget_remaining_tokens=-1,
        )

    t_ctx = ToolCallContext(
        run_id=RunId("run_abc"),
        step_id=StepId("step_01"),
        namespace_id=NamespaceId("tenant_x"),
        invocation_id=ToolInvocationId("inv_99"),
        idempotency_key="idemp_key_123",
        timeout_seconds=45.0,
    )
    assert ToolCallContext.from_json(t_ctx.to_json()) == t_ctx

    with pytest.raises(ValueError, match=r"timeout_seconds must be > 0\.0"):
        ToolCallContext(
            run_id=RunId("run_abc"),
            step_id=StepId("step_01"),
            namespace_id=NamespaceId("tenant_x"),
            invocation_id=ToolInvocationId("inv_99"),
            idempotency_key="idemp_key_123",
            timeout_seconds=0.0,
        )


def test_failure_details_validation_and_serialization() -> None:
    """FailureDetails preserves diagnostic details and roundtrips cleanly."""
    failure = FailureDetails(
        error_type="BudgetExceededError",
        message="Token limit of 10000 reached",
        step_id=StepId("step_05"),
        traceback_summary="Traceback: ...",
        recovery_hint="Increase token budget or enable compaction",
    )
    assert FailureDetails.from_json(failure.to_json()) == failure

    with pytest.raises(ValueError, match=r"error_type must not be empty"):
        FailureDetails(error_type="", message="Something went wrong")

    with pytest.raises(ValueError, match=r"message must not be empty"):
        FailureDetails(error_type="ValueError", message="   ")


def test_clock_and_id_generator_ports_runtime_check() -> None:
    """Classes adhering to ClockPort and IdGeneratorPort satisfy runtime protocol checks."""

    class SimpleClock:
        def now(self) -> datetime:
            return datetime.now(UTC)

    class SimpleIdGen:
        def generate_id(self, prefix: str = "") -> str:
            return f"{prefix}123"

    class IncompleteClock:
        pass

    assert isinstance(SimpleClock(), ClockPort)
    assert isinstance(SimpleIdGen(), IdGeneratorPort)
    assert not isinstance(IncompleteClock(), ClockPort)

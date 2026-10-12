"""Unit tests for AgentRuntime bounded execution loop runner."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from memory_aware_agent_harness.contracts import (
    CancellationToken,
    FailureDetails,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunBudgets,
    RunId,
    RunStatus,
    StepId,
    TokenUsage,
    ToolProtocol,
)
from memory_aware_agent_harness.doubles import DeterministicClock, DeterministicIdGenerator
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime


def test_agent_runtime_empty_task_raises() -> None:
    runtime = AgentRuntime(model=FakeModelAdapter())
    with pytest.raises(ValueError, match="Task description must not be empty"):
        runtime.run("", namespace_id=NamespaceId("ns"))

    with pytest.raises(ValueError, match="Task description must not be empty"):
        runtime.run("   \n\t  ", namespace_id=NamespaceId("ns"))


def test_agent_runtime_single_step_completion() -> None:
    clock = DeterministicClock()
    id_gen = DeterministicIdGenerator(prefix="test")
    model = FakeModelAdapter()

    runtime = AgentRuntime(
        model=model,
        clock=clock,
        id_generator=id_gen,
    )

    result = runtime.run("Answer the query", namespace_id=NamespaceId("ns_01"))

    assert result.status == RunStatus.COMPLETED
    assert result.step_count == 1
    assert result.namespace_id == "ns_01"
    assert result.total_tokens_consumed > 0
    assert "Deterministic completion" in (result.final_output or "")
    assert result.error is None
    assert result.failure_details is None
    assert model.call_count == 1


def test_agent_runtime_budget_exhaustion() -> None:
    # Model queues 5 steps without stopping
    decisions = [
        ModelDecision(
            decision_id=f"dec_{i}",
            step_id=StepId(f"step_{i}"),
            finish_reason=ModelFinishReason.LENGTH,
            content=f"Continuing step {i}",
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )
        for i in range(5)
    ]
    model = FakeModelAdapter(responses=decisions)

    # Budget allows max 2 steps
    runtime = AgentRuntime(model=model, budgets=RunBudgets(max_steps=2))

    result = runtime.run("Long task", namespace_id=NamespaceId("ns_02"))

    assert result.status == RunStatus.BUDGET_EXHAUSTED
    assert result.step_count == 2
    assert result.failure_details is not None
    assert result.failure_details.error_code == "BUDGET_EXCEEDED"


def test_agent_runtime_cancellation() -> None:
    token = CancellationToken()
    # Cancel token before start
    token.cancel(reason="Caller decided to stop")

    runtime = AgentRuntime(model=FakeModelAdapter())
    result = runtime.run(
        "Cancelled task",
        namespace_id=NamespaceId("ns_03"),
        cancellation_token=token,
    )

    assert result.status == RunStatus.CANCELLED
    assert result.step_count == 0
    assert result.failure_details is not None
    assert result.failure_details.error_code == "EXECUTION_CANCELLED"


def test_agent_runtime_model_error() -> None:
    failure = FailureDetails(
        error_type="ModelContentFiltered",
        message="Response triggered safety filter",
        error_code="SAFETY_FILTER",
    )
    decision = ModelDecision(
        decision_id="dec_err",
        step_id=StepId("step_01"),
        finish_reason=ModelFinishReason.ERROR,
        content="Filtered",
        failure_details=failure,
    )
    model = FakeModelAdapter(responses=[decision])
    runtime = AgentRuntime(model=model)

    result = runtime.run("Risky task", namespace_id=NamespaceId("ns_04"))

    assert result.status == RunStatus.FAILED
    assert result.failure_details is not None
    assert result.failure_details.error_code == "SAFETY_FILTER"
    assert result.error == "Response triggered safety filter"


def test_agent_runtime_exception_during_step() -> None:
    model = FakeModelAdapter(exception_to_raise=RuntimeError("Adapter crashed"))
    runtime = AgentRuntime(model=model)

    result = runtime.run("Crashing task", namespace_id=NamespaceId("ns_05"))

    assert result.status == RunStatus.FAILED
    assert result.failure_details is not None
    assert result.failure_details.error_type == "RuntimeError"
    assert "Adapter crashed" in (result.error or "")


def test_agent_runtime_explicit_run_id() -> None:
    runtime = AgentRuntime(model=FakeModelAdapter())
    result = runtime.run(
        "Task with custom ID",
        namespace_id=NamespaceId("ns_06"),
        run_id=RunId("custom_run_42"),
    )

    assert result.run_id == "custom_run_42"
    assert result.status == RunStatus.COMPLETED


def test_agent_runtime_pre_execution_validation() -> None:
    """P1-M4: Pre-execution validation rejects duplicates, invalid namespaces, and budgets."""
    tool1 = MagicMock(spec=ToolProtocol)
    tool1.name = "my_tool"
    tool2 = MagicMock(spec=ToolProtocol)
    tool2.name = "my_tool"

    # 1. Reject duplicate tool names
    with pytest.raises(ValueError, match="Duplicate tool registered"):
        AgentRuntime(model=FakeModelAdapter(), tools=[tool1, tool2])

    # 2. Reject empty tool name
    bad_tool = MagicMock(spec=ToolProtocol)
    bad_tool.name = "   "
    with pytest.raises(ValueError, match="Tool must have a non-empty name"):
        AgentRuntime(model=FakeModelAdapter(), tools=[bad_tool])

    # 3. Reject invalid budgets in __init__
    with pytest.raises(TypeError, match="budgets must be an instance of RunBudgets"):
        AgentRuntime(model=FakeModelAdapter(), budgets="invalid_budget")  # type: ignore[arg-type]

    runtime = AgentRuntime(model=FakeModelAdapter())

    # 4. Reject empty or whitespace namespace_id
    with pytest.raises(ValueError, match="Namespace ID must not be empty or whitespace"):
        runtime.run("Valid task", namespace_id=NamespaceId(""))

    with pytest.raises(ValueError, match="Namespace ID must not be empty or whitespace"):
        runtime.run("Valid task", namespace_id=NamespaceId("   \t  "))

    # 5. Reject empty or whitespace run_id
    with pytest.raises(ValueError, match="Run ID must not be empty or whitespace"):
        runtime.run("Valid task", namespace_id=NamespaceId("ns_01"), run_id=RunId("   "))

    # 6. Reject invalid budgets in run()
    with pytest.raises(TypeError, match="budgets must be an instance of RunBudgets"):
        runtime.run(
            "Valid task",
            namespace_id=NamespaceId("ns_01"),
            budgets={"max_steps": 5},  # type: ignore[arg-type]
        )

    # 7. Reject invalid cancellation_token in run()
    with pytest.raises(TypeError, match="cancellation_token must be an instance of"):
        runtime.run(
            "Valid task",
            namespace_id=NamespaceId("ns_01"),
            cancellation_token="not_a_token",  # type: ignore[arg-type]
        )

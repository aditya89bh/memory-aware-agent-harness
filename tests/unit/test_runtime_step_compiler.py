"""Unit tests for StepOutcomeCompiler."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memory_aware_agent_harness.contracts import (
    FailureDetails,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunId,
    SideEffectType,
    StepState,
    TokenUsage,
    ToolCallStatus,
    ToolInvocationId,
    ToolResult,
)
from memory_aware_agent_harness.doubles import DeterministicClock
from memory_aware_agent_harness.runtime.state import LifecycleState
from memory_aware_agent_harness.runtime.step_compiler import StepOutcomeCompiler


def test_step_outcome_compiler_standard_completion() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = DeterministicClock(start_time=t0)
    state = LifecycleState(
        run_id=RunId("r_comp_1"),
        namespace_id=NamespaceId("ns_comp"),
        clock=clock,
    )
    state.begin_run()
    step_id, step_fsm = state.begin_step()
    step_fsm.transition_to(StepState.RETRIEVING)
    step_fsm.transition_to(StepState.COMPILING_CONTEXT)
    step_fsm.transition_to(StepState.DECIDING)

    decision = ModelDecision(
        decision_id="dec_01",
        step_id=step_id,
        finish_reason=ModelFinishReason.STOP,
        content="Goal achieved",
        token_usage=TokenUsage(prompt_tokens=40, completion_tokens=10, total_tokens=50),
    )

    compiler = StepOutcomeCompiler(state)
    result = compiler.compile_step(decision=decision)

    assert result.step_id == step_id
    assert result.run_id == "r_comp_1"
    assert result.step_number == 1
    assert result.decision == decision
    assert result.tokens_consumed == 50
    assert result.token_usage == decision.token_usage
    assert result.tool_results == ()
    assert result.memories_proposed == 0
    assert result.memories_committed == 0
    assert result.failure_details is None
    assert step_fsm.state == StepState.COMPLETED
    assert state.cumulative_tokens.total_tokens == 50


def test_step_outcome_compiler_with_tools_and_memories() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = DeterministicClock(start_time=t0)
    state = LifecycleState(
        run_id=RunId("r_comp_2"),
        namespace_id=NamespaceId("ns_comp"),
        clock=clock,
    )
    state.begin_run()
    step_id, step_fsm = state.begin_step()
    step_fsm.transition_to(StepState.RETRIEVING)
    step_fsm.transition_to(StepState.COMPILING_CONTEXT)
    step_fsm.transition_to(StepState.DECIDING)
    step_fsm.transition_to(StepState.DISPATCHING_TOOL)
    step_fsm.transition_to(StepState.EXTRACTING_MEMORIES)

    decision = ModelDecision(
        decision_id="dec_02",
        step_id=step_id,
        finish_reason=ModelFinishReason.TOOL_CALL,
        content="Calling search",
        token_usage=TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )

    tool_res = ToolResult(
        invocation_id=ToolInvocationId("inv_01"),
        tool_name="search",
        status=ToolCallStatus.COMPLETED,
        side_effect=SideEffectType.READ_ONLY,
        idempotency_key="idemp_01",
        result={"hits": 3},
    )

    compiler = StepOutcomeCompiler(state)
    result = compiler.compile_step(
        decision=decision,
        tool_results=[tool_res],
        memories_proposed=3,
        memories_committed=2,
    )

    assert result.memories_proposed == 3
    assert result.memories_committed == 2
    assert len(result.tool_results) == 1
    assert result.tool_results[0] == tool_res
    assert step_fsm.state == StepState.COMPLETED
    assert state.cumulative_tokens.total_tokens == 120


def test_step_outcome_compiler_memory_count_validation() -> None:
    state = LifecycleState(run_id=RunId("r_comp_3"), namespace_id=NamespaceId("ns"))
    state.begin_run()
    step_id, _fsm = state.begin_step()

    decision = ModelDecision(
        decision_id="dec_03",
        step_id=step_id,
        finish_reason=ModelFinishReason.STOP,
        content="done",
    )
    compiler = StepOutcomeCompiler(state)

    with pytest.raises(ValueError, match=r"memories_committed .* cannot exceed memories_proposed"):
        compiler.compile_step(
            decision=decision,
            memories_proposed=2,
            memories_committed=5,
        )


def test_step_outcome_compiler_with_failure() -> None:
    state = LifecycleState(run_id=RunId("r_comp_4"), namespace_id=NamespaceId("ns"))
    state.begin_run()
    step_id, step_fsm = state.begin_step()
    step_fsm.transition_to(StepState.RETRIEVING)

    failure = FailureDetails(
        error_type="RetrievalTimeout",
        message="Memory store timed out",
        error_code="RETRIEVAL_TIMEOUT",
    )
    decision = ModelDecision(
        decision_id="dec_04",
        step_id=step_id,
        finish_reason=ModelFinishReason.ERROR,
        content="Error occurred",
        failure_details=failure,
    )

    compiler = StepOutcomeCompiler(state)
    result = compiler.compile_step(decision=decision, failure_details=failure)

    assert result.failure_details == failure
    assert step_fsm.state == StepState.FAILED

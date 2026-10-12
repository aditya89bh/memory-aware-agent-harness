"""Unit tests for RunResultBuilder."""

from __future__ import annotations

import pytest

from memory_aware_agent_harness.contracts import (
    FailureDetails,
    NamespaceId,
    RunId,
    RunStatus,
    StepState,
    TokenUsage,
)
from memory_aware_agent_harness.runtime.result_builder import RunResultBuilder
from memory_aware_agent_harness.runtime.state import LifecycleState


def test_run_result_builder_non_terminal_raises() -> None:
    state = LifecycleState(run_id=RunId("r_nb_1"), namespace_id=NamespaceId("ns"))
    builder = RunResultBuilder(state)

    # Initialized
    with pytest.raises(RuntimeError, match="non-terminal status 'initialized'"):
        builder.build()

    # Running
    state.begin_run()
    with pytest.raises(RuntimeError, match="non-terminal status 'running'"):
        builder.build()

    # Paused
    state.pause()
    with pytest.raises(RuntimeError, match="non-terminal status 'paused'"):
        builder.build()


def test_run_result_builder_completed() -> None:
    state = LifecycleState(run_id=RunId("r_comp_1"), namespace_id=NamespaceId("ns"))
    state.begin_run()
    _s1_id, s1_fsm = state.begin_step()
    s1_fsm.transition_to(StepState.RETRIEVING)
    s1_fsm.transition_to(StepState.COMPILING_CONTEXT)
    s1_fsm.transition_to(StepState.DECIDING)
    s1_fsm.transition_to(StepState.COMPLETED)

    state.record_tokens(TokenUsage(prompt_tokens=80, completion_tokens=20, total_tokens=100))
    state.complete()

    builder = RunResultBuilder(state)
    result = builder.build(final_output="Task completed successfully", trace_id="tr_custom_1")

    assert result.run_id == "r_comp_1"
    assert result.namespace_id == "ns"
    assert result.status == RunStatus.COMPLETED
    assert result.step_count == 1
    assert result.total_tokens_consumed == 100
    assert result.final_output == "Task completed successfully"
    assert result.trace_id == "tr_custom_1"
    assert result.error is None
    assert result.failure_details is None
    assert result.token_usage == state.cumulative_tokens


def test_run_result_builder_failed() -> None:
    state = LifecycleState(run_id=RunId("r_fail_1"), namespace_id=NamespaceId("ns"))
    state.begin_run()
    failure = FailureDetails(
        error_type="SandboxCrash",
        message="Process segfaulted in sandbox",
        error_code="SANDBOX_CRASH",
    )
    state.fail(failure)

    builder = RunResultBuilder(state)
    result = builder.build()

    assert result.status == RunStatus.FAILED
    assert result.error == "Process segfaulted in sandbox"
    assert result.failure_details == failure


def test_run_result_builder_aborted() -> None:
    state = LifecycleState(run_id=RunId("r_abort_1"), namespace_id=NamespaceId("ns"))
    state.begin_run()
    failure = FailureDetails(
        error_type="PolicyViolationError",
        message="Unauthorized network access",
        error_code="POLICY_VIOLATION",
    )
    state.abort(failure)

    builder = RunResultBuilder(state)
    result = builder.build()

    assert result.status == RunStatus.ABORTED
    assert result.error == "Unauthorized network access"
    assert result.failure_details == failure


def test_run_result_builder_cancelled_and_budget_exhausted() -> None:
    # Cancelled
    state_can = LifecycleState(run_id=RunId("r_can"), namespace_id=NamespaceId("ns"))
    state_can.begin_run()
    state_can.cancel(reason="Caller cancellation")

    res_can = RunResultBuilder(state_can).build()
    assert res_can.status == RunStatus.CANCELLED

    # Budget exhausted
    state_bg = LifecycleState(run_id=RunId("r_bg"), namespace_id=NamespaceId("ns"))
    state_bg.begin_run()
    state_bg.exhaust_budget(reason="Step limit hit")

    res_bg = RunResultBuilder(state_bg).build()
    assert res_bg.status == RunStatus.BUDGET_EXHAUSTED

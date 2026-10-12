"""Unit tests for LifecycleState container."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memory_aware_agent_harness.contracts import (
    CancellationToken,
    FailureDetails,
    NamespaceId,
    RunBudgets,
    RunId,
    RunStatus,
    StepState,
    TokenUsage,
)
from memory_aware_agent_harness.doubles import DeterministicClock, DeterministicIdGenerator
from memory_aware_agent_harness.runtime.state import (
    LifecycleState,
    SystemClock,
    SystemIdGenerator,
)


def test_system_clock_and_id_generator() -> None:
    clock = SystemClock()
    t1 = clock.now()
    assert t1.tzinfo is not None

    id_gen = SystemIdGenerator()
    id1 = id_gen.generate_id("test")
    assert id1.startswith("test_")
    id2 = id_gen.generate_id()
    assert len(id2) == 12


def test_lifecycle_state_initialization() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = DeterministicClock(start_time=t0)
    id_gen = DeterministicIdGenerator(prefix="test")
    token = CancellationToken()

    state = LifecycleState(
        run_id=RunId("run_001"),
        namespace_id=NamespaceId("ns_default"),
        budgets=RunBudgets(max_steps=10),
        clock=clock,
        id_generator=id_gen,
        cancellation_token=token,
    )

    assert state.run_id == "run_001"
    assert state.namespace_id == "ns_default"
    assert state.status == RunStatus.INITIALIZED
    assert not state.is_terminal
    assert state.step_count == 0
    assert state.current_step_id is None
    assert state.current_step_fsm is None
    assert state.cumulative_tokens == TokenUsage.zero()
    assert state.started_at == t0
    assert state.completed_at is None
    assert state.failure_details is None


def test_lifecycle_state_run_lifecycle_and_steps() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = DeterministicClock(start_time=t0)
    id_gen = DeterministicIdGenerator(prefix="test")

    state = LifecycleState(
        run_id=RunId("run_001"),
        namespace_id=NamespaceId("ns_default"),
        clock=clock,
        id_generator=id_gen,
    )

    # Cannot begin step before run starts
    with pytest.raises(RuntimeError, match="expected RUNNING"):
        state.begin_step()

    # Start run
    assert state.begin_run() == RunStatus.RUNNING
    assert state.status == RunStatus.RUNNING

    # Begin step 1
    s1_id, s1_fsm = state.begin_step()
    assert state.step_count == 1
    assert state.current_step_id == s1_id
    assert s1_fsm.state == StepState.NOT_STARTED

    # Cannot begin step 2 while step 1 is active
    with pytest.raises(RuntimeError, match="still active"):
        state.begin_step()

    # Complete step 1
    s1_fsm.transition_to(StepState.RETRIEVING)
    s1_fsm.transition_to(StepState.COMPILING_CONTEXT)
    s1_fsm.transition_to(StepState.DECIDING)
    s1_fsm.transition_to(StepState.COMPLETED)
    assert s1_fsm.is_terminal

    # Record tokens
    usage1 = TokenUsage(prompt_tokens=50, completion_tokens=25, total_tokens=75)
    cum1 = state.record_tokens(usage1)
    assert cum1 == usage1
    assert state.cumulative_tokens == usage1

    # Begin step 2
    s2_id, s2_fsm = state.begin_step()
    assert state.step_count == 2
    assert s2_id != s1_id
    assert s2_fsm.state == StepState.NOT_STARTED

    # Complete step 2
    s2_fsm.transition_to(StepState.FAILED)

    # Pause and resume
    p_status = state.pause()
    assert p_status == RunStatus.PAUSED
    r_status = state.resume()
    assert r_status == RunStatus.RUNNING

    # Advance clock and complete
    clock.advance(timedelta(seconds=12.5))
    assert state.complete() == RunStatus.COMPLETED
    assert state.is_terminal
    assert state.completed_at == t0 + timedelta(seconds=12.5)
    assert state.elapsed_seconds == 12.5


def test_lifecycle_state_terminal_outcomes() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    # Exhaust budget
    s_exhaust = LifecycleState(
        run_id=RunId("r_ex"),
        namespace_id=NamespaceId("ns"),
        clock=DeterministicClock(start_time=t0),
    )
    s_exhaust.begin_run()
    assert s_exhaust.exhaust_budget() == RunStatus.BUDGET_EXHAUSTED
    assert s_exhaust.is_terminal

    # Cancel
    token = CancellationToken()
    s_cancel = LifecycleState(
        run_id=RunId("r_can"),
        namespace_id=NamespaceId("ns"),
        clock=DeterministicClock(start_time=t0),
        cancellation_token=token,
    )
    s_cancel.begin_run()
    assert s_cancel.cancel(reason="user requested") == RunStatus.CANCELLED
    assert s_cancel.is_terminal
    assert token.is_cancelled

    # Abort
    s_abort = LifecycleState(
        run_id=RunId("r_ab"),
        namespace_id=NamespaceId("ns"),
        clock=DeterministicClock(start_time=t0),
    )
    s_abort.begin_run()
    failure_abort = FailureDetails(error_type="SecurityPolicyError", message="Forbidden tool")
    assert s_abort.abort(failure_abort) == RunStatus.ABORTED
    assert s_abort.failure_details == failure_abort

    # Fail
    s_fail = LifecycleState(
        run_id=RunId("r_fl"),
        namespace_id=NamespaceId("ns"),
        clock=DeterministicClock(start_time=t0),
    )
    s_fail.begin_run()
    failure_err = FailureDetails(error_type="RuntimeCrash", message="Out of memory")
    assert s_fail.fail(failure_err) == RunStatus.FAILED
    assert s_fail.failure_details == failure_err

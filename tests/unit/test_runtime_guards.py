"""Unit tests for runtime budget exhaustion guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memory_aware_agent_harness.contracts import (
    BudgetExceededError,
    NamespaceId,
    RunBudgets,
    RunId,
    RunStatus,
    StepState,
    TokenUsage,
)
from memory_aware_agent_harness.doubles import DeterministicClock
from memory_aware_agent_harness.runtime.guards import BudgetGuard
from memory_aware_agent_harness.runtime.state import LifecycleState


def test_budget_guard_step_limit_breach() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = DeterministicClock(start_time=t0)
    budgets = RunBudgets(max_steps=2, max_tokens=1000, max_time_seconds=60.0)

    state = LifecycleState(
        run_id=RunId("r1"),
        namespace_id=NamespaceId("ns1"),
        budgets=budgets,
        clock=clock,
    )
    state.begin_run()

    guard = BudgetGuard()

    # Step 0: healthy
    guard.check_step_budget(state)
    assert guard.evaluate_budget(state) is None

    # Step 1: healthy
    _s1_id, s1_fsm = state.begin_step()
    s1_fsm.transition_to(StepState.RETRIEVING)
    s1_fsm.transition_to(StepState.COMPILING_CONTEXT)
    s1_fsm.transition_to(StepState.DECIDING)
    s1_fsm.transition_to(StepState.COMPLETED)
    assert state.step_count == 1
    assert guard.evaluate_budget(state) is None

    # Step 2: reached limit
    _s2_id, s2_fsm = state.begin_step()
    s2_fsm.transition_to(StepState.RETRIEVING)
    s2_fsm.transition_to(StepState.COMPILING_CONTEXT)
    s2_fsm.transition_to(StepState.DECIDING)
    s2_fsm.transition_to(StepState.COMPLETED)
    assert state.step_count == 2

    # Step budget breach
    with pytest.raises(BudgetExceededError) as exc_info:
        guard.check_step_budget(state)
    assert exc_info.value.budget_type == "steps"
    assert exc_info.value.limit == 2
    assert exc_info.value.observed == 2

    # enforce() transitions to BUDGET_EXHAUSTED
    assert not guard.enforce(state)
    assert state.status == RunStatus.BUDGET_EXHAUSTED
    assert state.failure_details is not None
    assert state.failure_details.error_type == "BudgetExceededError"


def test_budget_guard_token_limit_breach() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = DeterministicClock(start_time=t0)
    budgets = RunBudgets(max_steps=10, max_tokens=100, max_time_seconds=60.0)

    state = LifecycleState(
        run_id=RunId("r2"),
        namespace_id=NamespaceId("ns1"),
        budgets=budgets,
        clock=clock,
    )
    state.begin_run()

    guard = BudgetGuard()

    # Consume 90 tokens: within budget
    state.record_tokens(TokenUsage(prompt_tokens=60, completion_tokens=30, total_tokens=90))
    guard.check_token_budget(state)
    assert guard.enforce(state)

    # Consume additional 20 tokens: total 110 > 100 limit
    state.record_tokens(TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20))
    assert state.cumulative_tokens.total_tokens == 110

    with pytest.raises(BudgetExceededError) as exc_info:
        guard.check_token_budget(state)
    assert exc_info.value.budget_type == "tokens"
    assert exc_info.value.limit == 100
    assert exc_info.value.observed == 110

    # enforce() transitions run to BUDGET_EXHAUSTED
    assert not guard.enforce(state)
    assert state.status == RunStatus.BUDGET_EXHAUSTED


def test_budget_guard_time_limit_breach() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = DeterministicClock(start_time=t0)
    budgets = RunBudgets(max_steps=10, max_tokens=1000, max_time_seconds=5.0)

    state = LifecycleState(
        run_id=RunId("r3"),
        namespace_id=NamespaceId("ns1"),
        budgets=budgets,
        clock=clock,
    )
    state.begin_run()

    guard = BudgetGuard()

    # Advance 4.0s: healthy
    clock.advance(timedelta(seconds=4.0))
    guard.check_time_budget(state)
    assert guard.enforce(state)

    # Advance another 2.0s: elapsed 6.0s > 5.0s
    clock.advance(timedelta(seconds=2.0))
    with pytest.raises(BudgetExceededError) as exc_info:
        guard.check_time_budget(state)
    assert exc_info.value.budget_type == "time"
    assert exc_info.value.limit == 5.0
    assert exc_info.value.observed == 6.0

    assert not guard.enforce(state)
    assert state.status == RunStatus.BUDGET_EXHAUSTED


def test_lifecycle_state_budget_methods() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = DeterministicClock(start_time=t0)
    budgets = RunBudgets(max_steps=1, max_tokens=50, max_time_seconds=10.0)

    state = LifecycleState(
        run_id=RunId("r4"),
        namespace_id=NamespaceId("ns1"),
        budgets=budgets,
        clock=clock,
    )
    state.begin_run()

    exhausted, exc = state.is_budget_exhausted()
    assert not exhausted
    assert exc is None

    # Exceed step count
    _s1_id, s1_fsm = state.begin_step()
    s1_fsm.transition_to(StepState.RETRIEVING)
    s1_fsm.transition_to(StepState.COMPILING_CONTEXT)
    s1_fsm.transition_to(StepState.DECIDING)
    s1_fsm.transition_to(StepState.COMPLETED)

    exhausted2, exc2 = state.is_budget_exhausted()
    assert exhausted2
    assert exc2 is not None
    assert exc2.budget_type == "steps"

    with pytest.raises(BudgetExceededError):
        state.check_budgets()

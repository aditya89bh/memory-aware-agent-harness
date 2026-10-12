"""Unit tests for RunFSM and StepFSM."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memory_aware_agent_harness.contracts import (
    InvalidStateTransitionError,
    RunStatus,
    StepState,
)
from memory_aware_agent_harness.doubles import DeterministicClock
from memory_aware_agent_harness.runtime.fsm import RunFSM, StepFSM


def test_run_fsm_initial_state_and_defaults() -> None:
    fsm = RunFSM()
    assert fsm.state == RunStatus.INITIALIZED
    assert fsm.current_state == RunStatus.INITIALIZED
    assert not fsm.is_terminal
    assert len(fsm.history) == 1
    assert fsm.history[0][0] == RunStatus.INITIALIZED
    assert fsm.history[0][2] is None


def test_run_fsm_with_clock() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = DeterministicClock(start_time=t0)
    fsm = RunFSM(clock=clock)
    assert fsm.history[0][1] == t0

    clock.advance(timedelta(seconds=5))
    fsm.transition_to(RunStatus.RUNNING, reason="started")
    assert fsm.state == RunStatus.RUNNING
    assert len(fsm.history) == 2
    assert fsm.history[1] == (RunStatus.RUNNING, t0 + timedelta(seconds=5), "started")


def test_run_fsm_happy_path_transitions() -> None:
    fsm = RunFSM()
    assert fsm.can_transition_to(RunStatus.RUNNING)
    assert fsm.can_transition_to("running")
    assert not fsm.can_transition_to(RunStatus.COMPLETED)

    s1 = fsm.transition_to(RunStatus.RUNNING)
    assert s1 == RunStatus.RUNNING

    # Self-transition in running loop
    s2 = fsm.transition_to(RunStatus.RUNNING)
    assert s2 == RunStatus.RUNNING

    # Pause and resume
    s3 = fsm.transition_to(RunStatus.PAUSED)
    assert s3 == RunStatus.PAUSED
    s4 = fsm.transition_to(RunStatus.RUNNING)
    assert s4 == RunStatus.RUNNING

    # Complete
    s5 = fsm.transition_to(RunStatus.COMPLETED)
    assert s5 == RunStatus.COMPLETED
    assert fsm.is_terminal


def test_run_fsm_all_terminal_states() -> None:
    terminal_statuses = [
        RunStatus.COMPLETED,
        RunStatus.BUDGET_EXHAUSTED,
        RunStatus.CANCELLED,
        RunStatus.ABORTED,
        RunStatus.FAILED,
    ]
    for term in terminal_statuses:
        fsm = RunFSM(initial_state=RunStatus.RUNNING)
        fsm.transition_to(term)
        assert fsm.state == term
        assert fsm.is_terminal
        # Terminal state cannot transition to anything
        assert not fsm.can_transition_to(RunStatus.RUNNING)
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            fsm.transition_to(RunStatus.RUNNING)
        assert exc_info.value.from_state == term.value
        assert exc_info.value.to_state == RunStatus.RUNNING.value


def test_run_fsm_illegal_transition_raises() -> None:
    fsm = RunFSM(initial_state=RunStatus.INITIALIZED)
    with pytest.raises(InvalidStateTransitionError) as exc_info:
        fsm.transition_to(RunStatus.COMPLETED)
    assert exc_info.value.from_state == RunStatus.INITIALIZED.value
    assert exc_info.value.to_state == RunStatus.COMPLETED.value


def test_step_fsm_initial_state_and_defaults() -> None:
    fsm = StepFSM()
    assert fsm.state == StepState.NOT_STARTED
    assert fsm.current_state == StepState.NOT_STARTED
    assert not fsm.is_terminal
    assert len(fsm.history) == 1
    assert fsm.history[0][0] == StepState.NOT_STARTED


def test_step_fsm_full_lifecycle_path() -> None:
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    clock = DeterministicClock(start_time=t0)
    fsm = StepFSM(clock=clock)

    phases = [
        StepState.RETRIEVING,
        StepState.COMPILING_CONTEXT,
        StepState.DECIDING,
        StepState.DISPATCHING_TOOL,
        StepState.EXTRACTING_MEMORIES,
        StepState.COMMITTING_MEMORIES,
        StepState.CHECKPOINTING,
        StepState.COMPLETED,
    ]

    for p in phases:
        assert fsm.can_transition_to(p)
        fsm.transition_to(p)
        assert fsm.state == p

    assert fsm.is_terminal
    assert len(fsm.history) == len(phases) + 1


def test_step_fsm_direct_completion_shortcuts() -> None:
    # Direct completion from DECIDING (e.g. no tool call)
    fsm = StepFSM()
    fsm.transition_to(StepState.RETRIEVING)
    fsm.transition_to(StepState.COMPILING_CONTEXT)
    fsm.transition_to(StepState.DECIDING)
    fsm.transition_to(StepState.COMPLETED)
    assert fsm.state == StepState.COMPLETED
    assert fsm.is_terminal

    # Direct completion from EXTRACTING_MEMORIES
    fsm2 = StepFSM()
    fsm2.transition_to(StepState.RETRIEVING)
    fsm2.transition_to(StepState.COMPILING_CONTEXT)
    fsm2.transition_to(StepState.DECIDING)
    fsm2.transition_to(StepState.EXTRACTING_MEMORIES)
    fsm2.transition_to(StepState.COMPLETED)
    assert fsm2.state == StepState.COMPLETED

    # Direct completion from COMMITTING_MEMORIES (skip checkpoint)
    fsm3 = StepFSM()
    fsm3.transition_to(StepState.RETRIEVING)
    fsm3.transition_to(StepState.COMPILING_CONTEXT)
    fsm3.transition_to(StepState.DECIDING)
    fsm3.transition_to(StepState.EXTRACTING_MEMORIES)
    fsm3.transition_to(StepState.COMMITTING_MEMORIES)
    fsm3.transition_to(StepState.COMPLETED)
    assert fsm3.state == StepState.COMPLETED


def test_step_fsm_error_and_abort_transitions() -> None:
    # Failure from NOT_STARTED
    fsm = StepFSM()
    fsm.transition_to(StepState.FAILED, reason="pre-step crash")
    assert fsm.state == StepState.FAILED
    assert fsm.is_terminal

    # Abort from DECIDING
    fsm2 = StepFSM()
    fsm2.transition_to(StepState.RETRIEVING)
    fsm2.transition_to(StepState.COMPILING_CONTEXT)
    fsm2.transition_to(StepState.DECIDING)
    fsm2.transition_to(StepState.ABORTED, reason="policy abort")
    assert fsm2.state == StepState.ABORTED
    assert fsm2.is_terminal


def test_step_fsm_illegal_transition_raises() -> None:
    fsm = StepFSM()
    with pytest.raises(InvalidStateTransitionError) as exc_info:
        fsm.transition_to(StepState.CHECKPOINTING)
    assert exc_info.value.from_state == StepState.NOT_STARTED.value
    assert exc_info.value.to_state == StepState.CHECKPOINTING.value

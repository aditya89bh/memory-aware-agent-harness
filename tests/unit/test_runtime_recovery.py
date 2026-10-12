"""Unit tests for orphaned run recovery handler."""

from __future__ import annotations

import pytest

from memory_aware_agent_harness.contracts import (
    NamespaceId,
    RunId,
    RunStatus,
    StepState,
)
from memory_aware_agent_harness.runtime.recovery import (
    OrphanedRunError,
    OrphanedRunRecoveryHandler,
)
from memory_aware_agent_harness.runtime.state import LifecycleState


def test_orphaned_run_error_attributes() -> None:
    err = OrphanedRunError()
    assert err.error_code == "ORPHANED_RUN_DETECTED"
    assert err.recoverable
    assert "recovery_hint" in err.details


def test_orphaned_run_recovery_to_failed() -> None:
    state = LifecycleState(
        run_id=RunId("r_rec_1"),
        namespace_id=NamespaceId("ns_rec"),
    )
    state.begin_run()
    _s1_id, s1_fsm = state.begin_step()
    s1_fsm.transition_to(StepState.RETRIEVING)
    s1_fsm.transition_to(StepState.COMPILING_CONTEXT)

    handler = OrphanedRunRecoveryHandler(state)
    assert handler.is_orphaned

    # Recover to failed
    status = handler.recover_to_failed(reason="Node rebooted abruptly")
    assert status == RunStatus.FAILED
    assert s1_fsm.state == StepState.FAILED
    assert state.failure_details is not None
    assert state.failure_details.error_type == "OrphanedRunError"
    assert state.failure_details.error_code == "ORPHANED_RUN_DETECTED"


def test_orphaned_run_recovery_idempotent_on_terminal() -> None:
    state = LifecycleState(
        run_id=RunId("r_rec_term"),
        namespace_id=NamespaceId("ns_rec"),
    )
    state.begin_run()
    handler = OrphanedRunRecoveryHandler(state)
    handler.recover_to_failed()
    assert handler.recover_to_failed() == RunStatus.FAILED


def test_orphaned_run_recovery_to_paused_and_resume() -> None:
    state = LifecycleState(
        run_id=RunId("r_rec_2"),
        namespace_id=NamespaceId("ns_rec"),
    )
    state.begin_run()
    _s1_id, s1_fsm = state.begin_step()
    s1_fsm.transition_to(StepState.RETRIEVING)

    handler = OrphanedRunRecoveryHandler(state)
    assert handler.is_orphaned

    # Recover to paused
    status = handler.recover_to_paused(reason="Awaiting operator verification")
    assert status == RunStatus.PAUSED
    assert s1_fsm.state == StepState.FAILED
    assert state.failure_details is not None
    assert state.failure_details.error_type == "OrphanedRunPaused"

    # From paused, we can resume execution
    resume_status = state.resume(reason="Operator approved continuation")
    assert resume_status == RunStatus.RUNNING

    # Can now start a fresh step
    _s2_id, s2_fsm = state.begin_step()
    assert s2_fsm.state == StepState.NOT_STARTED


def test_orphaned_run_recovery_strategy_dispatch() -> None:
    # Test strategy = 'fail'
    state1 = LifecycleState(run_id=RunId("r_st_1"), namespace_id=NamespaceId("ns"))
    state1.begin_run()
    h1 = OrphanedRunRecoveryHandler(state1)
    assert h1.recover(strategy="fail") == RunStatus.FAILED

    # Test strategy = 'pause'
    state2 = LifecycleState(run_id=RunId("r_st_2"), namespace_id=NamespaceId("ns"))
    state2.begin_run()
    h2 = OrphanedRunRecoveryHandler(state2)
    assert h2.recover(strategy="pause") == RunStatus.PAUSED

    # Test invalid strategy
    with pytest.raises(ValueError, match="Unknown recovery strategy"):
        h2.recover(strategy="invalid")  # type: ignore[arg-type]

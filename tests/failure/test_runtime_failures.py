"""Comprehensive negative path and failure tests for runtime lifecycle and FSMs."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from memory_aware_agent_harness.contracts import (
    FailureDetails,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunId,
    RunStatus,
    StepState,
)
from memory_aware_agent_harness.runtime.cancellation import (
    AbortDispatcher,
)
from memory_aware_agent_harness.runtime.fsm import RunFSM, StepFSM
from memory_aware_agent_harness.runtime.recovery import OrphanedRunRecoveryHandler
from memory_aware_agent_harness.runtime.result_builder import RunResultBuilder
from memory_aware_agent_harness.runtime.state import LifecycleState
from memory_aware_agent_harness.runtime.step_compiler import StepOutcomeCompiler


def test_fsm_invalid_enum_types_and_values() -> None:
    run_fsm = RunFSM()
    with pytest.raises(TypeError, match="must be an instance of RunStatus or str"):
        run_fsm.transition_to(123)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must be a valid RunStatus"):
        run_fsm.transition_to("non_existent_status")

    step_fsm = StepFSM()
    with pytest.raises(TypeError, match="must be an instance of StepState or str"):
        step_fsm.transition_to(None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must be a valid StepState"):
        step_fsm.transition_to("invalid_phase")


def test_abort_dispatcher_exception_suppression_and_terminal_branches() -> None:
    state = LifecycleState(run_id=RunId("r_ab_edge"), namespace_id=NamespaceId("ns"))
    state.begin_run()
    _step_id, _step_fsm = state.begin_step()

    # Mock step_fsm.transition_to to raise an unexpected exception
    bad_fsm = MagicMock()
    bad_fsm.is_terminal = False
    bad_fsm.transition_to.side_effect = RuntimeError("Unforeseen transition crash")
    state.current_step_fsm = bad_fsm

    dispatcher = AbortDispatcher(state)
    # abort_active_step catches and logs warning without re-raising
    dispatcher.abort_active_step()

    # dispatch_cancellation when token is already cancelled
    state.cancellation_token.cancel("prior cancel")
    assert dispatcher.dispatch_cancellation("prior cancel") == RunStatus.CANCELLED

    # dispatch_policy_abort with a generic RuntimeError instead of PolicyViolationError
    state_pol = LifecycleState(run_id=RunId("r_pol"), namespace_id=NamespaceId("ns"))
    state_pol.begin_run()
    disp_pol = AbortDispatcher(state_pol)
    status = disp_pol.dispatch_policy_abort(RuntimeError("Generic guard violation"))
    assert status == RunStatus.ABORTED
    assert state_pol.failure_details is not None
    assert state_pol.failure_details.error_code == "POLICY_VIOLATION"

    # dispatch_policy_abort when state is already terminal
    status_terminal = disp_pol.dispatch_policy_abort(RuntimeError("Late violation"))
    assert status_terminal == RunStatus.ABORTED

    # dispatch_abort with default reason
    state_ab = LifecycleState(run_id=RunId("r_ab_def"), namespace_id=NamespaceId("ns"))
    state_ab.begin_run()
    disp_ab = AbortDispatcher(state_ab)
    fd = FailureDetails(error_type="Custom", message="Custom abort message")
    assert disp_ab.dispatch_abort(fd) == RunStatus.ABORTED

    # dispatch_abort when state is already terminal
    assert disp_ab.dispatch_abort(fd) == RunStatus.ABORTED


def test_orphaned_run_recovery_edge_cases() -> None:
    # Exception suppression in _fail_active_step
    state = LifecycleState(run_id=RunId("r_rec_edge"), namespace_id=NamespaceId("ns"))
    state.begin_run()
    state.begin_step()

    bad_fsm = MagicMock()
    bad_fsm.is_terminal = False
    bad_fsm.transition_to.side_effect = RuntimeError("Corrupted step FSM")
    state.current_step_fsm = bad_fsm

    handler = OrphanedRunRecoveryHandler(state)
    assert handler.recover_to_failed() == RunStatus.FAILED

    # recover_to_paused when run is in INITIALIZED state
    state_init = LifecycleState(run_id=RunId("r_init_rec"), namespace_id=NamespaceId("ns"))
    handler_init = OrphanedRunRecoveryHandler(state_init)
    paused_status = handler_init.recover_to_paused("Paused before start")
    assert paused_status == RunStatus.PAUSED


def test_run_result_builder_fallback_failure_details() -> None:
    # FAILED state with error provided but failure_details is None
    state_fail = LifecycleState(run_id=RunId("r_fail_fallback"), namespace_id=NamespaceId("ns"))
    state_fail.begin_run()
    state_fail.run_fsm.transition_to(RunStatus.FAILED)

    builder = RunResultBuilder(state_fail)
    res = builder.build(error="Explicit error string passed")
    assert res.error == "Explicit error string passed"
    assert res.failure_details is not None
    assert res.failure_details.message == "Explicit error string passed"

    # FAILED state with neither error nor failure_details provided
    state_fail2 = LifecycleState(run_id=RunId("r_fail_none"), namespace_id=NamespaceId("ns"))
    state_fail2.begin_run()
    state_fail2.run_fsm.transition_to(RunStatus.FAILED)
    res2 = RunResultBuilder(state_fail2).build()
    assert res2.failure_details is not None
    assert "Run terminated in status failed" in res2.failure_details.message

    # CANCELLED state with reason=None
    state_can = LifecycleState(run_id=RunId("r_can_none"), namespace_id=NamespaceId("ns"))
    state_can.begin_run()
    state_can.cancel()
    res_can = RunResultBuilder(state_can).build()
    assert res_can.status == RunStatus.CANCELLED


def test_step_compiler_without_active_step_id_and_terminal_fsm() -> None:
    state = LifecycleState(run_id=RunId("r_comp_edge"), namespace_id=NamespaceId("ns"))
    state.begin_run()
    # Notice we do NOT call state.begin_step(), so current_step_id is None
    compiler = StepOutcomeCompiler(state)

    decision = ModelDecision(
        decision_id="dec_fallback",
        step_id="step_synthetic",  # type: ignore[arg-type]
        finish_reason=ModelFinishReason.STOP,
        content="Direct compile",
    )
    res = compiler.compile_step(decision=decision)
    assert res.step_id is not None
    assert res.tokens_consumed == 0

    # Test compilation when step FSM is already terminal
    step_id, step_fsm = state.begin_step()
    step_fsm.transition_to(StepState.RETRIEVING)
    step_fsm.transition_to(StepState.FAILED)
    assert step_fsm.is_terminal

    decision2 = ModelDecision(
        decision_id="dec_term",
        step_id=step_id,
        finish_reason=ModelFinishReason.ERROR,
        content="failed",
        failure_details=FailureDetails(error_type="Error", message="fail"),
    )
    res2 = compiler.compile_step(decision=decision2)
    assert res2.step_id == step_id

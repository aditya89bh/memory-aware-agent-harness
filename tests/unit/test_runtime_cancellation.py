"""Unit tests for live cancellation listener and abort dispatcher."""

from __future__ import annotations

import pytest

from memory_aware_agent_harness.contracts import (
    CancellationToken,
    ExecutionCancelledError,
    FailureDetails,
    NamespaceId,
    PolicyViolationError,
    RunId,
    RunStatus,
    StepState,
)
from memory_aware_agent_harness.runtime.cancellation import (
    AbortDispatcher,
    CancellationListener,
)
from memory_aware_agent_harness.runtime.state import LifecycleState


def test_cancellation_listener_when_uncancelled() -> None:
    token = CancellationToken()
    state = LifecycleState(
        run_id=RunId("r_can_1"),
        namespace_id=NamespaceId("ns_can"),
        cancellation_token=token,
    )
    state.begin_run()
    _s1_id, s1_fsm = state.begin_step()
    s1_fsm.transition_to(StepState.RETRIEVING)

    listener = CancellationListener(state)
    assert not listener.is_cancelled

    # check() does not raise when uncancelled
    listener.check()

    # abort_if_cancelled() returns False when uncancelled
    assert not listener.abort_if_cancelled()
    assert state.status == RunStatus.RUNNING
    assert s1_fsm.state == StepState.RETRIEVING


def test_cancellation_listener_when_cancelled() -> None:
    token = CancellationToken()
    token.cancel(reason="manual stop")
    state = LifecycleState(
        run_id=RunId("r_can_2"),
        namespace_id=NamespaceId("ns_can"),
        cancellation_token=token,
    )
    state.begin_run()
    _s1_id, s1_fsm = state.begin_step()
    s1_fsm.transition_to(StepState.RETRIEVING)

    listener = CancellationListener(state)
    assert listener.is_cancelled

    with pytest.raises(ExecutionCancelledError, match="manual stop"):
        listener.check()

    # abort_if_cancelled() executes abort
    assert listener.abort_if_cancelled()
    assert state.status == RunStatus.CANCELLED
    assert s1_fsm.state == StepState.ABORTED
    assert state.failure_details is not None
    assert state.failure_details.error_code == "EXECUTION_CANCELLED"


def test_abort_dispatcher_active_step() -> None:
    state = LifecycleState(
        run_id=RunId("r_disp_1"),
        namespace_id=NamespaceId("ns_disp"),
    )
    state.begin_run()
    _s1_id, s1_fsm = state.begin_step()
    s1_fsm.transition_to(StepState.RETRIEVING)
    s1_fsm.transition_to(StepState.COMPILING_CONTEXT)
    s1_fsm.transition_to(StepState.DECIDING)

    dispatcher = AbortDispatcher(state)
    dispatcher.abort_active_step(reason="step abort test")

    assert s1_fsm.state == StepState.ABORTED
    # Aborting an already terminal step does not raise
    dispatcher.abort_active_step(reason="redundant abort")
    assert s1_fsm.state == StepState.ABORTED


def test_abort_dispatcher_policy_abort() -> None:
    state = LifecycleState(
        run_id=RunId("r_disp_2"),
        namespace_id=NamespaceId("ns_disp"),
    )
    state.begin_run()
    _s1_id, s1_fsm = state.begin_step()
    s1_fsm.transition_to(StepState.RETRIEVING)

    dispatcher = AbortDispatcher(state)
    policy_err = PolicyViolationError("Disallowed tool execution", policy_name="sandbox_policy")

    final_status = dispatcher.dispatch_policy_abort(policy_err, policy_name="sandbox_policy")

    assert final_status == RunStatus.ABORTED
    assert state.status == RunStatus.ABORTED
    assert s1_fsm.state == StepState.ABORTED
    assert state.failure_details is not None
    assert state.failure_details.error_type == "PolicyViolationError"


def test_abort_dispatcher_custom_abort() -> None:
    state = LifecycleState(
        run_id=RunId("r_disp_3"),
        namespace_id=NamespaceId("ns_disp"),
    )
    state.begin_run()
    _s1_id, s1_fsm = state.begin_step()
    s1_fsm.transition_to(StepState.RETRIEVING)

    dispatcher = AbortDispatcher(state)
    custom_failure = FailureDetails(
        error_type="CustomAbort",
        message="Critical safety intercept",
        error_code="CRITICAL_SAFETY",
    )

    final_status = dispatcher.dispatch_abort(custom_failure)

    assert final_status == RunStatus.ABORTED
    assert state.status == RunStatus.ABORTED
    assert s1_fsm.state == StepState.ABORTED
    assert state.failure_details == custom_failure

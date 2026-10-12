"""Live cancellation listener and abort dispatcher for runtime execution.

Provides cooperative cancellation detection, abort dispatching, and atomic
transition coordination across active StepFSM and RunFSM instances.
"""

from __future__ import annotations

import logging

from memory_aware_agent_harness.contracts import (
    CancellationToken,
    FailureDetails,
    PolicyViolationError,
    RunStatus,
    StepState,
)
from memory_aware_agent_harness.runtime.state import LifecycleState

logger = logging.getLogger(__name__)


class CancellationListener:
    """Monitors live cancellation token and coordinates step-level cooperative aborts."""

    def __init__(self, state: LifecycleState) -> None:
        self.state = state
        self._token: CancellationToken = state.cancellation_token

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation has been requested on the underlying token."""
        return self._token.is_cancelled

    def check(self) -> None:
        """Check for cancellation.

        Raises:
            ExecutionCancelledError: If cancellation was requested.
        """
        self._token.check_cancellation()

    def abort_if_cancelled(self) -> bool:
        """Check cancellation; if cancelled, dispatch abort and return True.

        Returns:
            True if execution was cancelled and aborted.
            False if execution is uncancelled and may proceed.
        """
        if not self._token.is_cancelled:
            return False

        reason = self._token.reason or "Execution cancelled by caller"
        dispatcher = AbortDispatcher(self.state)
        dispatcher.dispatch_cancellation(reason=reason)
        return True


class AbortDispatcher:
    """Dispatches terminal aborts across active step and run lifecycles."""

    def __init__(self, state: LifecycleState) -> None:
        self.state = state

    def abort_active_step(self, reason: str | None = None) -> None:
        """Transition active step FSM to ABORTED if currently non-terminal."""
        step_fsm = self.state.current_step_fsm
        if step_fsm is not None and not step_fsm.is_terminal:
            try:
                step_fsm.transition_to(StepState.ABORTED, reason=reason)
            except Exception as exc:
                logger.warning("Failed to transition active step to ABORTED: %s", exc)

    def dispatch_cancellation(self, reason: str = "Execution cancelled") -> RunStatus:
        """Atomically abort the active step and transition run to CANCELLED."""
        self.abort_active_step(reason=reason)

        if not self.state.cancellation_token.is_cancelled:
            self.state.cancellation_token.cancel(reason=reason)

        failure = FailureDetails(
            error_type="ExecutionCancelledError",
            message=reason,
            step_id=self.state.current_step_id,
            error_code="EXECUTION_CANCELLED",
            recovery_hint="Re-run task with new parameters or resume from checkpoint",
        )
        self.state.failure_details = failure

        if not self.state.is_terminal:
            return self.state.run_fsm.transition_to(RunStatus.CANCELLED, reason=reason)
        return self.state.status

    def dispatch_policy_abort(
        self,
        violation: PolicyViolationError | Exception,
        policy_name: str = "security_policy",
    ) -> RunStatus:
        """Atomically abort the active step and transition run to ABORTED for policy violations."""
        reason = str(violation)
        self.abort_active_step(reason=reason)

        failure: FailureDetails
        if isinstance(violation, PolicyViolationError):
            failure = violation.to_failure_details(step_id=self.state.current_step_id)
        else:
            failure = FailureDetails(
                error_type=type(violation).__name__,
                message=reason,
                step_id=self.state.current_step_id,
                error_code="POLICY_VIOLATION",
                recovery_hint=f"Review violation of policy '{policy_name}' and adjust task inputs",
            )
        self.state.failure_details = failure

        if not self.state.is_terminal:
            return self.state.abort(failure, reason=reason)
        return self.state.status

    def dispatch_abort(
        self,
        failure: FailureDetails,
        reason: str | None = None,
    ) -> RunStatus:
        """Atomically abort active step and transition run to ABORTED with custom FailureDetails."""
        eff_reason = reason or failure.message
        self.abort_active_step(reason=eff_reason)
        if not self.state.is_terminal:
            return self.state.abort(failure, reason=eff_reason)
        return self.state.status

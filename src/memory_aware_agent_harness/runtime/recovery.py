"""Orphaned run recovery handler and process crash reconciliation.

Provides legal FSM reconciliation for runs and steps left in uncompleted states
following unexpected process termination, crash, or node failure.
"""

from __future__ import annotations

import logging
from typing import Literal

from memory_aware_agent_harness.contracts import (
    FailureDetails,
    HarnessError,
    RunStatus,
    StepState,
)
from memory_aware_agent_harness.runtime.state import LifecycleState

logger = logging.getLogger(__name__)


class OrphanedRunError(HarnessError):
    """Raised when an orphaned execution run is detected and rejected."""

    def __init__(
        self,
        message: str = "Active run was orphaned due to unexpected process termination",
    ) -> None:
        super().__init__(
            message,
            error_code="ORPHANED_RUN_DETECTED",
            details={
                "error_code": "ORPHANED_RUN_DETECTED",
                "recovery_hint": (
                    "Inspect step audit log and idempotency keys before retrying or replaying"
                ),
            },
            recoverable=True,
        )


class OrphanedRunRecoveryHandler:
    """Reconciles orphaned runs into legal terminal or paused recovery states."""

    def __init__(self, state: LifecycleState) -> None:
        self.state = state

    @property
    def is_orphaned(self) -> bool:
        """Whether the run is currently in an uncompleted, non-terminal state."""
        return self.state.status in (RunStatus.RUNNING, RunStatus.INITIALIZED)

    def _fail_active_step(self, reason: str) -> None:
        """Transition any in-flight non-terminal step to FAILED."""
        step_fsm = self.state.current_step_fsm
        if step_fsm is not None and not step_fsm.is_terminal:
            try:
                step_fsm.transition_to(StepState.FAILED, reason=reason)
            except Exception as exc:
                logger.warning("Failed to transition orphaned step to FAILED: %s", exc)

    def recover_to_failed(
        self,
        reason: str = "Active run orphaned due to abrupt process termination",
    ) -> RunStatus:
        """Reconcile an orphaned run to FAILED status with diagnostic FailureDetails.

        If already terminal, returns the current status idempotently.
        """
        if self.state.is_terminal:
            return self.state.status

        self._fail_active_step(reason=reason)

        failure = FailureDetails(
            error_type="OrphanedRunError",
            message=reason,
            step_id=self.state.current_step_id,
            error_code="ORPHANED_RUN_DETECTED",
            recovery_hint=(
                "Inspect step audit log and external idempotency keys before retrying or replaying"
            ),
        )
        return self.state.fail(failure, reason=reason)

    def recover_to_paused(
        self,
        reason: str = "Active run paused for operator intervention following process restart",
    ) -> RunStatus:
        """Reconcile an orphaned run to PAUSED status awaiting operator intervention.

        If already terminal, returns the current status idempotently.
        """
        if self.state.is_terminal:
            return self.state.status

        self._fail_active_step(reason=reason)

        failure = FailureDetails(
            error_type="OrphanedRunPaused",
            message=reason,
            step_id=self.state.current_step_id,
            error_code="ORPHANED_RUN_PAUSED",
            recovery_hint=("Verify uncommitted state, reconcile tool outcomes, then call resume()"),
        )
        self.state.failure_details = failure

        if self.state.status != RunStatus.PAUSED:
            if self.state.status == RunStatus.INITIALIZED:
                # Transition INITIALIZED -> RUNNING -> PAUSED for legal FSM sequence
                self.state.begin_run()
            self.state.pause(reason=reason)

        return self.state.status

    def recover(
        self,
        strategy: Literal["fail", "pause"] = "fail",
        reason: str | None = None,
    ) -> RunStatus:
        """Reconcile orphaned run using the specified strategy ('fail' or 'pause')."""
        if strategy == "fail":
            return self.recover_to_failed(
                reason=reason or "Active run orphaned due to abrupt process termination"
            )
        if strategy == "pause":
            return self.recover_to_paused(
                reason=reason or "Active run paused for operator intervention"
            )
        raise ValueError(f"Unknown recovery strategy '{strategy}', expected 'fail' or 'pause'")

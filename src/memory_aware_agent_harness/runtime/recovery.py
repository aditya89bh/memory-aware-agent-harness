"""Orphaned run recovery handler and process crash reconciliation.

Provides legal FSM reconciliation for runs and steps left in uncompleted states
following unexpected process termination, crash, or node failure.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Literal

from memory_aware_agent_harness.contracts import (
    DirtyRollbackError,
    FailureDetails,
    HarnessError,
    NamespaceId,
    OrphanedRunReport,
    RecordNotFoundError,
    RunId,
    RunStatus,
    SideEffectType,
    StepState,
    ToolCallStatus,
)
from memory_aware_agent_harness.runtime.state import LifecycleState
from memory_aware_agent_harness.storage.connection import transaction
from memory_aware_agent_harness.storage.runs_repo import (
    fail_orphaned_step,
    get_run,
    list_orphaned_runs,
    list_running_steps_for_run,
    update_run_status,
)
from memory_aware_agent_harness.storage.tools_repo import (
    ToolInvocationRecord,
    find_pending_or_unknown_invocations,
    mark_invocation_unknown,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DirtyRollbackError",
    "OrphanedRunError",
    "OrphanedRunRecoveryHandler",
    "OrphanedRunReport",
    "reconcile_orphaned_run",
    "reconcile_orphaned_runs",
]


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


def reconcile_orphaned_run(
    conn: sqlite3.Connection,
    run_id: RunId,
    namespace_id: NamespaceId,
    *,
    strategy: Literal["fail", "pause"] = "fail",
    reason: str | None = None,
) -> OrphanedRunReport:
    """Reconcile a single orphaned execution run by ID with tenant namespace scoping.

    Audits in-flight steps and mutating tool invocations:
    - If in-flight mutating tool invocations exist, marks run dirty and records
      PARTIAL_DIRTY_ROLLBACK diagnostic.
    - Transitions all pending tool invocations to OUTCOME_UNKNOWN.
    - Transitions any in-flight running steps to FAILED.
    - Transitions run to FAILED (or PAUSED).

    Args:
        conn: Target SQLite connection.
        run_id: Identifier of the run to reconcile.
        namespace_id: Tenant namespace.
        strategy: Recovery strategy ('fail' or 'pause').
        reason: Optional diagnostic explanation.

    Returns:
        OrphanedRunReport detailing the reconciliation outcome.

    Raises:
        ValueError: If strategy is invalid.
        RecordNotFoundError: If run is not found.
        CrossNamespaceAccessError: If run belongs to another namespace.
    """
    if strategy not in ("fail", "pause"):
        raise ValueError(f"Unknown recovery strategy '{strategy}', expected 'fail' or 'pause'")

    with transaction(conn):
        run = get_run(conn, run_id, namespace_id)
        if run is None:
            raise RecordNotFoundError(
                f"Run '{run_id}' not found in namespace '{namespace_id}'",
                record_id=str(run_id),
                namespace_id=str(namespace_id),
                table="runs",
            )

        # If already terminal, return idempotent report
        if run.status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.CANCELLED,
        ):
            return OrphanedRunReport(
                run_id=run.run_id,
                namespace_id=run.namespace_id,
                previous_status=run.status,
                new_status=run.status,
                is_dirty=False,
                mutating_tool_invocations_count=0,
                reconciled_steps_count=0,
                reason="Run is already in terminal state",
                failure_details=run.failure_details
                or FailureDetails(
                    error_type="RunTerminal",
                    message="Run is already in terminal state",
                    error_code="RUN_ALREADY_TERMINAL",
                ),
            )

        # 1. Audit tool invocations for this run
        pending_or_unknown = find_pending_or_unknown_invocations(
            conn, namespace_id, run_id=run.run_id
        )

        mutating_invocations: list[ToolInvocationRecord] = []
        for ti in pending_or_unknown:
            if (
                ti.side_effect
                in (
                    SideEffectType.IDEMPOTENT_WRITE,
                    SideEffectType.NON_IDEMPOTENT_WRITE,
                )
                or ti.status == ToolCallStatus.OUTCOME_UNKNOWN
            ):
                mutating_invocations.append(ti)

        is_dirty = len(mutating_invocations) > 0

        # 2. Mark any pending tool invocations as OUTCOME_UNKNOWN
        for ti in pending_or_unknown:
            if ti.status == ToolCallStatus.PENDING:
                mark_invocation_unknown(
                    conn,
                    ti.invocation_id,
                    namespace_id,
                    error="Invocation outcome uncertain due to unexpected process crash",
                )

        # 3. Transition in-flight running steps to FAILED
        running_step_ids = list_running_steps_for_run(conn, run.run_id, namespace_id)
        for s_id in running_step_ids:
            rollback_kind = "dirty rollback" if is_dirty else "clean rollback"
            fail_orphaned_step(
                conn,
                s_id,
                namespace_id,
                reason=f"Step interrupted by process crash ({rollback_kind})",
                failure_details=FailureDetails(
                    error_type="OrphanedStepError",
                    message=f"Step aborted due to process crash (dirty={is_dirty})",
                    step_id=s_id,
                    error_code="PARTIAL_DIRTY_ROLLBACK" if is_dirty else "ORPHANED_STEP_DETECTED",
                    recovery_hint="Inspect step execution log and tool outcomes before retrying",
                ),
            )

        # 4. Determine new status and failure details
        now = datetime.now(UTC)
        if strategy == "pause":
            target_status = RunStatus.PAUSED
            completed_at = None
        else:
            target_status = RunStatus.FAILED
            completed_at = now

        if is_dirty:
            error_code = "PARTIAL_DIRTY_ROLLBACK"
            error_type = "DirtyRollbackError"
            msg = reason or (
                f"Active run orphaned with {len(mutating_invocations)} unconfirmed mutating "
                f"tool invocation(s); reconciled to {target_status.value} (dirty rollback)"
            )
            recovery_hint = (
                "In-flight mutating tool side-effects cannot be safely inferred. "
                "Resuming execution requires compensation handlers or operator reconciliation."
            )
        else:
            error_code = (
                "ORPHANED_RUN_DETECTED"
                if target_status == RunStatus.FAILED
                else "ORPHANED_RUN_PAUSED"
            )
            error_type = "OrphanedRunError"
            msg = reason or (
                f"Active run orphaned due to process termination; "
                f"reconciled to {target_status.value}"
            )
            recovery_hint = (
                "Inspect step audit log and external idempotency keys before retrying or replaying."
            )

        last_step_id = (
            running_step_ids[-1]
            if running_step_ids
            else (run.step_history[-1].step_id if run.step_history else None)
        )

        fd = FailureDetails(
            error_type=error_type,
            message=msg,
            step_id=last_step_id,
            error_code=error_code,
            recovery_hint=recovery_hint,
        )

        update_run_status(
            conn,
            run_id=run.run_id,
            namespace_id=namespace_id,
            status=target_status,
            error=msg,
            failure_details=fd,
            completed_at=completed_at,
        )

        return OrphanedRunReport(
            run_id=run.run_id,
            namespace_id=namespace_id,
            previous_status=run.status,
            new_status=target_status,
            is_dirty=is_dirty,
            mutating_tool_invocations_count=len(mutating_invocations),
            reconciled_steps_count=len(running_step_ids),
            reason=msg,
            failure_details=fd,
        )


def reconcile_orphaned_runs(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId,
    *,
    strategy: Literal["fail", "pause"] = "fail",
    reason: str | None = None,
) -> tuple[OrphanedRunReport, ...]:
    """Scan and reconcile all orphaned runs within a tenant namespace."""
    orphaned_runs = list_orphaned_runs(conn, namespace_id)
    reports: list[OrphanedRunReport] = []
    for run in orphaned_runs:
        rep = reconcile_orphaned_run(
            conn,
            run.run_id,
            namespace_id,
            strategy=strategy,
            reason=reason,
        )
        reports.append(rep)
    return tuple(reports)

"""Lifecycle state container tracking active run, step, and status transitions.

Coordinates RunFSM, StepFSM, token accounting, step counting, timestamps,
and cancellation tokens across the execution lifecycle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    CancellationToken,
    ClockPort,
    FailureDetails,
    IdGeneratorPort,
    NamespaceId,
    RunBudgets,
    RunId,
    RunStatus,
    StepId,
    StepState,
    TokenUsage,
)
from memory_aware_agent_harness.runtime.fsm import RunFSM, StepFSM


class SystemClock(ClockPort):
    """Default system clock providing timezone-aware UTC instants."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class SystemIdGenerator(IdGeneratorPort):
    """Default system ID generator producing UUID-based unique identifiers."""

    def generate_id(self, prefix: str = "") -> str:
        uid = uuid.uuid4().hex[:12]
        if prefix:
            sep = "" if prefix.endswith(("_", "-")) else "_"
            return f"{prefix}{sep}{uid}"
        return uid


class LifecycleState:
    """Encapsulates mutable runtime state and coordinates FSM lifecycles."""

    def __init__(
        self,
        run_id: RunId,
        namespace_id: NamespaceId,
        budgets: RunBudgets | None = None,
        clock: ClockPort | None = None,
        id_generator: IdGeneratorPort | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        self.run_id = run_id
        self.namespace_id = namespace_id
        self.budgets = budgets if budgets is not None else RunBudgets()
        self.clock = clock if clock is not None else SystemClock()
        self.id_generator = id_generator if id_generator is not None else SystemIdGenerator()
        self.cancellation_token = (
            cancellation_token if cancellation_token is not None else CancellationToken()
        )

        self.run_fsm = RunFSM(initial_state=RunStatus.INITIALIZED, clock=self.clock)
        self.step_count = 0
        self.current_step_id: StepId | None = None
        self.current_step_fsm: StepFSM | None = None
        self.cumulative_tokens = TokenUsage.zero()

        self.started_at: datetime = self.clock.now()
        self.completed_at: datetime | None = None
        self.failure_details: FailureDetails | None = None
        self.metadata: dict[str, Any] = {}

    @property
    def status(self) -> RunStatus:
        """Current status of the run."""
        return self.run_fsm.state

    @property
    def is_terminal(self) -> bool:
        """Whether the run is in a terminal status."""
        return self.run_fsm.is_terminal

    @property
    def elapsed_seconds(self) -> float:
        """Elapsed seconds from start to completion or current clock instant."""
        end_time = self.completed_at if self.completed_at is not None else self.clock.now()
        delta = end_time - self.started_at
        return max(0.0, delta.total_seconds())

    def begin_run(self) -> RunStatus:
        """Start run execution, transitioning from INITIALIZED to RUNNING."""
        return self.run_fsm.transition_to(RunStatus.RUNNING, reason="run started")

    def begin_step(self) -> tuple[StepId, StepFSM]:
        """Start a new step execution, advancing the step counter and initializing StepFSM.

        Raises:
            RuntimeError: If the run is not actively in RUNNING status, or previous step
                is non-terminal.
        """
        if self.status != RunStatus.RUNNING:
            raise RuntimeError(
                f"Cannot begin step when run is in status '{self.status.value}', expected RUNNING"
            )
        if self.current_step_fsm is not None and not self.current_step_fsm.is_terminal:
            raise RuntimeError(
                f"Cannot begin step: previous step '{self.current_step_id}' "
                f"is still active in phase '{self.current_step_fsm.state.value}'"
            )

        self.step_count += 1
        step_id_str = self.id_generator.generate_id(f"step_{self.run_id}_{self.step_count}")
        step_id = StepId(step_id_str)
        step_fsm = StepFSM(initial_state=StepState.NOT_STARTED, clock=self.clock)

        self.current_step_id = step_id
        self.current_step_fsm = step_fsm
        return step_id, step_fsm

    def record_tokens(self, usage: TokenUsage) -> TokenUsage:
        """Accumulate token consumption, ensuring monotonic non-negative addition."""
        self.cumulative_tokens = self.cumulative_tokens + usage
        return self.cumulative_tokens

    def pause(self, reason: str | None = None) -> RunStatus:
        """Pause run execution."""
        return self.run_fsm.transition_to(RunStatus.PAUSED, reason=reason)

    def resume(self, reason: str | None = None) -> RunStatus:
        """Resume paused run execution."""
        return self.run_fsm.transition_to(RunStatus.RUNNING, reason=reason)

    def complete(self, reason: str | None = None) -> RunStatus:
        """Mark run as completed successfully."""
        status = self.run_fsm.transition_to(RunStatus.COMPLETED, reason=reason)
        self.completed_at = self.clock.now()
        return status

    def exhaust_budget(self, reason: str | None = None) -> RunStatus:
        """Mark run as budget exhausted."""
        status = self.run_fsm.transition_to(RunStatus.BUDGET_EXHAUSTED, reason=reason)
        self.completed_at = self.clock.now()
        return status

    def cancel(self, reason: str | None = None) -> RunStatus:
        """Mark run as cancelled."""
        if reason is not None:
            self.cancellation_token.cancel(reason=reason)
        else:
            self.cancellation_token.cancel()
        status = self.run_fsm.transition_to(RunStatus.CANCELLED, reason=reason)
        self.completed_at = self.clock.now()
        return status

    def abort(self, failure: FailureDetails, reason: str | None = None) -> RunStatus:
        """Mark run as aborted due to policy/safety violation."""
        self.failure_details = failure
        status = self.run_fsm.transition_to(RunStatus.ABORTED, reason=reason)
        self.completed_at = self.clock.now()
        return status

    def fail(self, failure: FailureDetails, reason: str | None = None) -> RunStatus:
        """Mark run as failed due to unhandled error or crash."""
        self.failure_details = failure
        status = self.run_fsm.transition_to(RunStatus.FAILED, reason=reason)
        self.completed_at = self.clock.now()
        return status

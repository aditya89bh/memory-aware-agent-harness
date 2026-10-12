"""Finite State Machines for Agent Run and Step Lifecycles.

Enforces legal state transitions using transition matrices defined in contracts.
Raises InvalidStateTransitionError on illegal or terminal state transitions.
"""

from __future__ import annotations

from datetime import UTC, datetime

from memory_aware_agent_harness.contracts import (
    LEGAL_RUN_TRANSITIONS,
    LEGAL_STEP_TRANSITIONS,
    ClockPort,
    InvalidStateTransitionError,
    RunStatus,
    StepState,
    _coerce_enum,
)

_TERMINAL_RUN_STATES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.BUDGET_EXHAUSTED,
        RunStatus.CANCELLED,
        RunStatus.ABORTED,
        RunStatus.FAILED,
    }
)

_TERMINAL_STEP_STATES: frozenset[StepState] = frozenset(
    {
        StepState.COMPLETED,
        StepState.FAILED,
        StepState.ABORTED,
    }
)


class RunFSM:
    """Finite State Machine controlling the lifecycle transitions of an agent run."""

    def __init__(
        self,
        initial_state: RunStatus | str = RunStatus.INITIALIZED,
        clock: ClockPort | None = None,
    ) -> None:
        self._clock = clock
        self._state = _coerce_enum(RunStatus, initial_state, "initial_state")
        now = self._get_now()
        self._history: list[tuple[RunStatus, datetime, str | None]] = [(self._state, now, None)]

    def _get_now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(UTC)

    @property
    def state(self) -> RunStatus:
        """Current lifecycle status of the run."""
        return self._state

    @property
    def current_state(self) -> RunStatus:
        """Alias for state property."""
        return self._state

    @property
    def is_terminal(self) -> bool:
        """Whether the run is in a terminal state."""
        return self._state in _TERMINAL_RUN_STATES

    @property
    def history(self) -> tuple[tuple[RunStatus, datetime, str | None], ...]:
        """Chronological audit history of state transitions."""
        return tuple(self._history)

    def can_transition_to(self, to_state: RunStatus | str) -> bool:
        """Check if transitioning to the target state is legally permitted."""
        target = _coerce_enum(RunStatus, to_state, "to_state")
        legal_targets = LEGAL_RUN_TRANSITIONS.get(self._state, frozenset())
        return target in legal_targets

    def transition_to(
        self,
        to_state: RunStatus | str,
        reason: str | None = None,
    ) -> RunStatus:
        """Transition the run FSM to a new status.

        Raises:
            InvalidStateTransitionError: If the transition is not legally permitted.
        """
        target = _coerce_enum(RunStatus, to_state, "to_state")
        legal_targets = LEGAL_RUN_TRANSITIONS.get(self._state, frozenset())

        if target not in legal_targets:
            raise InvalidStateTransitionError(
                f"Illegal run state transition from '{self._state.value}' to '{target.value}'",
                from_state=self._state.value,
                to_state=target.value,
            )

        self._state = target
        self._history.append((self._state, self._get_now(), reason))
        return self._state


class StepFSM:
    """Finite State Machine controlling the discrete execution phases of a single step."""

    def __init__(
        self,
        initial_state: StepState | str = StepState.NOT_STARTED,
        clock: ClockPort | None = None,
    ) -> None:
        self._clock = clock
        self._state = _coerce_enum(StepState, initial_state, "initial_state")
        now = self._get_now()
        self._history: list[tuple[StepState, datetime, str | None]] = [(self._state, now, None)]

    def _get_now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(UTC)

    @property
    def state(self) -> StepState:
        """Current execution phase of the step."""
        return self._state

    @property
    def current_state(self) -> StepState:
        """Alias for state property."""
        return self._state

    @property
    def is_terminal(self) -> bool:
        """Whether the step has concluded in a terminal phase."""
        return self._state in _TERMINAL_STEP_STATES

    @property
    def history(self) -> tuple[tuple[StepState, datetime, str | None], ...]:
        """Chronological audit history of state transitions."""
        return tuple(self._history)

    def can_transition_to(self, to_state: StepState | str) -> bool:
        """Check if transitioning to the target state is legally permitted."""
        target = _coerce_enum(StepState, to_state, "to_state")
        legal_targets = LEGAL_STEP_TRANSITIONS.get(self._state, frozenset())
        return target in legal_targets

    def transition_to(
        self,
        to_state: StepState | str,
        reason: str | None = None,
    ) -> StepState:
        """Transition the step FSM to a new phase.

        Raises:
            InvalidStateTransitionError: If the transition is not legally permitted.
        """
        target = _coerce_enum(StepState, to_state, "to_state")
        legal_targets = LEGAL_STEP_TRANSITIONS.get(self._state, frozenset())

        if target not in legal_targets:
            raise InvalidStateTransitionError(
                f"Illegal step state transition from '{self._state.value}' to '{target.value}'",
                from_state=self._state.value,
                to_state=target.value,
            )

        self._state = target
        self._history.append((self._state, self._get_now(), reason))
        return self._state

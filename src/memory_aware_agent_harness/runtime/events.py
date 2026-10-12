"""Diagnostic hooks and lifecycle event emission for AgentRuntime.

Provides immutable runtime event records, hook protocols, and an event emitter
that observes and dispatches runtime and step lifecycle transitions.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from memory_aware_agent_harness.contracts import (
    ClockPort,
    EventId,
    FrozenDict,
    IdGeneratorPort,
    RunId,
    SerializableRecord,
    StepId,
    StepState,
    _coerce_enum,
    _freeze,
    _parse_dt,
)

logger = logging.getLogger(__name__)


class RuntimeEventType(StrEnum):
    """Categorical types of runtime diagnostic lifecycle events."""

    RUN_STARTED = "run_started"
    STEP_STARTED = "step_started"
    PHASE_TRANSITION = "phase_transition"
    MODEL_INVOKED = "model_invoked"
    MODEL_RESPONDED = "model_responded"
    TOOL_INVOKED = "tool_invoked"
    TOOL_COMPLETED = "tool_completed"
    STEP_COMPLETED = "step_completed"
    RUN_TERMINATED = "run_terminated"


@dataclass(frozen=True)
class RuntimeEvent(SerializableRecord):
    """Immutable, serializable diagnostic event emitted during runtime lifecycle."""

    event_id: EventId
    event_type: RuntimeEventType
    run_id: RunId
    sequence_number: int
    timestamp: datetime
    step_id: StepId | None = None
    phase: StepState | None = None
    payload: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if self.sequence_number < 0:
            raise ValueError("sequence_number must be non-negative")

        object.__setattr__(
            self,
            "event_type",
            _coerce_enum(RuntimeEventType, self.event_type, "event_type"),
        )
        object.__setattr__(self, "timestamp", _parse_dt(self.timestamp))
        if self.phase is not None:
            object.__setattr__(self, "phase", _coerce_enum(StepState, self.phase, "phase"))
        if self.step_id is not None:
            if not isinstance(self.step_id, str) or not self.step_id.strip():
                raise ValueError("step_id must be a non-empty string if provided")
            object.__setattr__(self, "step_id", StepId(str(self.step_id)))

        object.__setattr__(self, "payload", _freeze(self.payload))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RuntimeEvent:
        """Construct RuntimeEvent instance from a dictionary."""
        phase_raw = data.get("phase")
        phase = StepState(str(phase_raw)) if phase_raw is not None else None
        step_id_raw = data.get("step_id")
        step_id = StepId(str(step_id_raw)) if step_id_raw is not None else None

        return cls(
            event_id=EventId(str(data["event_id"])),
            event_type=RuntimeEventType(str(data["event_type"])),
            run_id=RunId(str(data["run_id"])),
            sequence_number=int(data["sequence_number"]),
            timestamp=_parse_dt(data["timestamp"]),
            step_id=step_id,
            phase=phase,
            payload=_freeze(data.get("payload", {})),
        )


@runtime_checkable
class RuntimeEventListener(Protocol):
    """Protocol for observers listening to runtime diagnostic events."""

    def on_event(self, event: RuntimeEvent) -> None:
        """Process an emitted diagnostic runtime event."""
        ...


class DiagnosticHook:
    """Base class for diagnostic hooks dispatching to specific lifecycle callbacks."""

    def on_event(self, event: RuntimeEvent) -> None:
        """Dispatch event to specialized lifecycle callbacks."""
        match event.event_type:
            case RuntimeEventType.RUN_STARTED:
                self.on_run_started(event)
            case RuntimeEventType.STEP_STARTED:
                self.on_step_started(event)
            case RuntimeEventType.PHASE_TRANSITION:
                self.on_phase_transition(event)
            case RuntimeEventType.MODEL_INVOKED:
                self.on_model_invoked(event)
            case RuntimeEventType.MODEL_RESPONDED:
                self.on_model_responded(event)
            case RuntimeEventType.TOOL_INVOKED:
                self.on_tool_invoked(event)
            case RuntimeEventType.TOOL_COMPLETED:
                self.on_tool_completed(event)
            case RuntimeEventType.STEP_COMPLETED:
                self.on_step_completed(event)
            case RuntimeEventType.RUN_TERMINATED:
                self.on_run_terminated(event)

    def on_run_started(self, event: RuntimeEvent) -> None:
        """Callback invoked when a run starts."""

    def on_step_started(self, event: RuntimeEvent) -> None:
        """Callback invoked when a step starts."""

    def on_phase_transition(self, event: RuntimeEvent) -> None:
        """Callback invoked on step phase transition."""

    def on_model_invoked(self, event: RuntimeEvent) -> None:
        """Callback invoked before model invocation."""

    def on_model_responded(self, event: RuntimeEvent) -> None:
        """Callback invoked after model responds."""

    def on_tool_invoked(self, event: RuntimeEvent) -> None:
        """Callback invoked before tool execution."""

    def on_tool_completed(self, event: RuntimeEvent) -> None:
        """Callback invoked after tool execution completes."""

    def on_step_completed(self, event: RuntimeEvent) -> None:
        """Callback invoked when a step completes."""

    def on_run_terminated(self, event: RuntimeEvent) -> None:
        """Callback invoked when a run terminates."""


class EventCollector(DiagnosticHook):
    """In-memory collector accumulating diagnostic events for verification and debugging."""

    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    def on_event(self, event: RuntimeEvent) -> None:
        """Record event and dispatch to base hook callbacks."""
        self.events.append(event)
        super().on_event(event)

    def events_of_type(self, event_type: RuntimeEventType | str) -> list[RuntimeEvent]:
        """Filter captured events by event type."""
        target = RuntimeEventType(str(event_type))
        return [e for e in self.events if e.event_type == target]

    def clear(self) -> None:
        """Clear recorded events."""
        self.events.clear()


class DiagnosticEventEmitter:
    """Manages event sequence ordering and dispatch to registered observers."""

    def __init__(
        self,
        clock: ClockPort | None = None,
        id_generator: IdGeneratorPort | None = None,
        listeners: Sequence[RuntimeEventListener] = (),
        strict: bool = False,
    ) -> None:
        self._clock = clock
        self._id_generator = id_generator
        self._listeners: list[RuntimeEventListener] = list(listeners)
        self._strict = strict
        self._sequence_counter = 0
        self._history: list[RuntimeEvent] = []

    @property
    def history(self) -> tuple[RuntimeEvent, ...]:
        """Immutable sequence of all emitted events in temporal order."""
        return tuple(self._history)

    def add_listener(self, listener: RuntimeEventListener) -> None:
        """Register a diagnostic event listener."""
        self._listeners.append(listener)

    def remove_listener(self, listener: RuntimeEventListener) -> None:
        """Unregister a diagnostic event listener."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def emit(
        self,
        event_type: RuntimeEventType | str,
        run_id: RunId,
        step_id: StepId | None = None,
        phase: StepState | str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> RuntimeEvent:
        """Construct, record, and dispatch a diagnostic runtime event."""
        seq = self._sequence_counter
        self._sequence_counter += 1

        ts = self._clock.now() if self._clock is not None else datetime.now(UTC)

        if self._id_generator is not None:
            evt_id = EventId(self._id_generator.generate_id(f"evt_{seq}"))
        else:
            evt_id = EventId(f"evt_{seq}_{int(ts.timestamp() * 1000)}")

        eff_phase: StepState | None = None
        if phase is not None:
            eff_phase = _coerce_enum(StepState, phase, "phase")

        event = RuntimeEvent(
            event_id=evt_id,
            event_type=_coerce_enum(RuntimeEventType, event_type, "event_type"),
            run_id=run_id,
            sequence_number=seq,
            timestamp=ts,
            step_id=step_id,
            phase=eff_phase,
            payload=_freeze(payload) if payload else FrozenDict(),
        )

        self._history.append(event)
        self._dispatch(event)
        return event

    def _dispatch(self, event: RuntimeEvent) -> None:
        """Deliver event to all registered listeners."""
        for listener in self._listeners:
            try:
                listener.on_event(event)
            except Exception as exc:
                if self._strict:
                    raise
                logger.warning(
                    "Error in diagnostic event listener %s: %s",
                    type(listener).__name__,
                    exc,
                )

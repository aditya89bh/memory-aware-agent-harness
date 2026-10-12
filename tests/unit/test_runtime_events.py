"""Unit tests for diagnostic hooks and runtime lifecycle event emission."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    EventId,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunId,
    RunStatus,
    SideEffectType,
    StepId,
    StepState,
    ToolCallContext,
    ToolCallRequest,
    ToolProtocol,
)
from memory_aware_agent_harness.doubles import DeterministicClock, DeterministicIdGenerator
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.events import (
    DiagnosticEventEmitter,
    DiagnosticHook,
    EventCollector,
    RuntimeEvent,
    RuntimeEventListener,
    RuntimeEventType,
)
from memory_aware_agent_harness.runtime.runner import AgentRuntime


class _DummyTool(ToolProtocol):
    def __init__(self, name: str = "echo") -> None:
        self.name = name
        self.description = "Echo tool"
        self.side_effect = SideEffectType.READ_ONLY

    def get_schema(self) -> Mapping[str, Any]:
        return {"name": self.name}

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        return {"result": "ok"}


def test_runtime_event_validation_and_serialization() -> None:
    now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
    evt = RuntimeEvent(
        event_id=EventId("evt_0"),
        event_type=RuntimeEventType.RUN_STARTED,
        run_id=RunId("run_1"),
        sequence_number=0,
        timestamp=now,
        step_id=StepId("step_0"),
        phase=StepState.NOT_STARTED,
        payload={"key": "val"},
    )

    assert evt.event_id == "evt_0"
    assert evt.event_type == RuntimeEventType.RUN_STARTED
    assert evt.sequence_number == 0
    assert evt.payload["key"] == "val"

    # Serialization round-trip
    d = evt.to_dict()
    assert d["event_id"] == "evt_0"
    assert d["event_type"] == "run_started"
    assert d["sequence_number"] == 0

    reconstructed = RuntimeEvent.from_dict(d)
    assert reconstructed == evt

    # JSON round-trip
    json_str = evt.to_json()
    from_json = RuntimeEvent.from_json(json_str)
    assert from_json == evt


def test_runtime_event_validation_failures() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="event_id must be a non-empty string"):
        RuntimeEvent(
            event_id=EventId(""),
            event_type=RuntimeEventType.RUN_STARTED,
            run_id=RunId("r1"),
            sequence_number=0,
            timestamp=now,
        )

    with pytest.raises(ValueError, match="run_id must be a non-empty string"):
        RuntimeEvent(
            event_id=EventId("e1"),
            event_type=RuntimeEventType.RUN_STARTED,
            run_id=RunId("   "),
            sequence_number=0,
            timestamp=now,
        )

    with pytest.raises(ValueError, match="sequence_number must be non-negative"):
        RuntimeEvent(
            event_id=EventId("e1"),
            event_type=RuntimeEventType.RUN_STARTED,
            run_id=RunId("r1"),
            sequence_number=-1,
            timestamp=now,
        )

    with pytest.raises(ValueError, match="step_id must be a non-empty string if provided"):
        RuntimeEvent(
            event_id=EventId("e1"),
            event_type=RuntimeEventType.RUN_STARTED,
            run_id=RunId("r1"),
            sequence_number=0,
            timestamp=now,
            step_id=StepId("   "),
        )


def test_diagnostic_event_emitter_listeners_and_history() -> None:
    clock = DeterministicClock()
    id_gen = DeterministicIdGenerator(prefix="test")
    collector = EventCollector()

    emitter = DiagnosticEventEmitter(
        clock=clock,
        id_generator=id_gen,
        listeners=[collector],
    )

    e1 = emitter.emit(
        RuntimeEventType.RUN_STARTED,
        run_id=RunId("r_100"),
        payload={"info": "started"},
    )
    assert e1.sequence_number == 0
    assert e1.run_id == "r_100"

    e2 = emitter.emit(
        RuntimeEventType.STEP_STARTED,
        run_id=RunId("r_100"),
        step_id=StepId("s_100"),
        phase=StepState.NOT_STARTED,
    )
    assert e2.sequence_number == 1

    assert len(emitter.history) == 2
    assert emitter.history[0] == e1
    assert emitter.history[1] == e2

    # Collector received both events
    assert len(collector.events) == 2
    assert collector.events_of_type(RuntimeEventType.RUN_STARTED) == [e1]

    # Unregister listener
    emitter.remove_listener(collector)
    emitter.emit(RuntimeEventType.RUN_TERMINATED, run_id=RunId("r_100"))
    assert len(collector.events) == 2
    assert len(emitter.history) == 3


def test_diagnostic_event_emitter_error_handling() -> None:
    class _FailingListener(RuntimeEventListener):
        def on_event(self, event: RuntimeEvent) -> None:
            raise RuntimeError("Listener exploded")

    # Non-strict emitter suppresses listener exceptions
    emitter_lenient = DiagnosticEventEmitter(strict=False, listeners=[_FailingListener()])
    e = emitter_lenient.emit(RuntimeEventType.RUN_STARTED, run_id=RunId("r_safe"))
    assert e.run_id == "r_safe"

    # Strict emitter re-raises listener exceptions
    emitter_strict = DiagnosticEventEmitter(strict=True, listeners=[_FailingListener()])
    with pytest.raises(RuntimeError, match="Listener exploded"):
        emitter_strict.emit(RuntimeEventType.RUN_STARTED, run_id=RunId("r_strict"))


def test_agent_runtime_lifecycle_event_emission_e2e() -> None:
    collector = EventCollector()
    clock = DeterministicClock()
    id_gen = DeterministicIdGenerator(prefix="evtrun")

    tool = _DummyTool(name="test_tool")
    decision_step1 = ModelDecision(
        decision_id="dec_1",
        step_id=StepId("s_1"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(ToolCallRequest(call_id="c_1", tool_name="test_tool", arguments={}),),
    )
    decision_step2 = ModelDecision(
        decision_id="dec_2",
        step_id=StepId("s_2"),
        finish_reason=ModelFinishReason.STOP,
        content="All done",
    )
    model = FakeModelAdapter(responses=[decision_step1, decision_step2])

    runtime = AgentRuntime(
        model=model,
        tools=[tool],
        clock=clock,
        id_generator=id_gen,
        event_listeners=[collector],
    )

    result = runtime.run("Two step task", namespace_id=NamespaceId("ns_events"))
    assert result.status == RunStatus.COMPLETED

    event_types = [e.event_type for e in collector.events]

    # Verify key lifecycle event milestones
    assert RuntimeEventType.RUN_STARTED in event_types
    assert RuntimeEventType.STEP_STARTED in event_types
    assert RuntimeEventType.PHASE_TRANSITION in event_types
    assert RuntimeEventType.MODEL_INVOKED in event_types
    assert RuntimeEventType.MODEL_RESPONDED in event_types
    assert RuntimeEventType.TOOL_INVOKED in event_types
    assert RuntimeEventType.TOOL_COMPLETED in event_types
    assert RuntimeEventType.STEP_COMPLETED in event_types
    assert RuntimeEventType.RUN_TERMINATED in event_types

    # First event is RUN_STARTED, last is RUN_TERMINATED
    assert collector.events[0].event_type == RuntimeEventType.RUN_STARTED
    assert collector.events[-1].event_type == RuntimeEventType.RUN_TERMINATED

    # Monotonic sequence numbering
    for idx, e in enumerate(collector.events):
        assert e.sequence_number == idx

    # Check collector clear
    collector.clear()
    assert len(collector.events) == 0


def test_diagnostic_hook_callbacks_invoked() -> None:
    class _CustomHook(DiagnosticHook):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def on_run_started(self, event: RuntimeEvent) -> None:
            self.calls.append("run_started")

        def on_step_started(self, event: RuntimeEvent) -> None:
            self.calls.append("step_started")

        def on_phase_transition(self, event: RuntimeEvent) -> None:
            self.calls.append("phase_transition")

        def on_model_invoked(self, event: RuntimeEvent) -> None:
            self.calls.append("model_invoked")

        def on_model_responded(self, event: RuntimeEvent) -> None:
            self.calls.append("model_responded")

        def on_step_completed(self, event: RuntimeEvent) -> None:
            self.calls.append("step_completed")

        def on_run_terminated(self, event: RuntimeEvent) -> None:
            self.calls.append("run_terminated")

    hook = _CustomHook()
    runtime = AgentRuntime(
        model=FakeModelAdapter(),
        event_listeners=[hook],
    )
    result = runtime.run("Single step task", namespace_id=NamespaceId("ns_hook"))
    assert result.status == RunStatus.COMPLETED

    assert "run_started" in hook.calls
    assert "step_started" in hook.calls
    assert "phase_transition" in hook.calls
    assert "model_invoked" in hook.calls
    assert "model_responded" in hook.calls
    assert "step_completed" in hook.calls
    assert "run_terminated" in hook.calls

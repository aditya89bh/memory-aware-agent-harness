"""Unit tests for nested argument serialization at the storage boundary (Task 2)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    FrozenDict,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunId,
    SerializationError,
    SideEffectType,
    StepId,
    ToolCallContext,
    ToolCallRequest,
    ToolCallStatus,
    ToolProtocol,
    to_canonical_json,
)
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter


class NestedPayloadTool(ToolProtocol):
    """Tool that inspects and echoes nested arguments."""

    def __init__(self, name: str = "nested_tool") -> None:
        self.name = name
        self.description = "Processes complex nested arguments"
        self.side_effect = SideEffectType.READ_ONLY
        self.executed_arguments: list[Mapping[str, Any]] = []

    def get_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {"n": {"type": "object"}},
            "required": ["n"],
        }

    def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolCallContext,
    ) -> Mapping[str, Any]:
        self.executed_arguments.append(arguments)
        return {"received": dict(arguments), "status": "processed"}


def test_tool_call_request_with_nested_frozendict_serialization() -> None:
    """Claude reproduction: ToolCallRequest('c', 't', {'n': {'a': 1}}) serializes cleanly."""
    req = ToolCallRequest("c", "t", {"n": {"a": 1}})
    assert isinstance(req.arguments["n"], FrozenDict)

    serialized = to_canonical_json(req.arguments)
    assert serialized == '{"n":{"a":1}}'


def test_to_canonical_json_nested_primitives_and_collections() -> None:
    """Verify canonical serialization of lists, tuples, sets, and primitives."""
    payload = {
        "str": "hello",
        "int": 42,
        "float": 3.14,
        "bool": True,
        "none": None,
        "list": [1, 2, ("nested", "tuple")],
        "set": {"b", "a"},
        "nested_dict": {"x": 10, "y": [20, 30]},
    }
    serialized = to_canonical_json(payload)
    assert '"str":"hello"' in serialized
    assert '"set":["a","b"]' in serialized
    assert '"list":[1,2,["nested","tuple"]]' in serialized


def test_to_canonical_json_rejects_unsupported_types() -> None:
    """Arbitrary unsupported types are rejected with structured SerializationError."""

    class CustomObject:
        pass

    with pytest.raises(SerializationError) as exc_info:
        to_canonical_json({"bad": CustomObject()})
    assert exc_info.value.error_code == "SERIALIZATION_ERROR"
    assert "Unsupported" in exc_info.value.message


def test_to_canonical_json_rejects_non_finite_floats() -> None:
    """Non-finite floats (NaN, Inf) are rejected with SerializationError."""
    with pytest.raises(SerializationError) as exc_info:
        to_canonical_json({"nan": float("nan")})
    assert exc_info.value.error_code == "SERIALIZATION_ERROR"
    assert "finite" in exc_info.value.message.lower()

    with pytest.raises(SerializationError) as exc_info2:
        to_canonical_json({"inf": float("inf")})
    assert exc_info2.value.error_code == "SERIALIZATION_ERROR"


def test_identical_tool_execution_with_and_without_storage() -> None:
    """Tool execution produces identical results with and without SQLite storage attached."""
    nested_args = {"n": {"a": 1, "items": [10, 20]}, "tag": "test"}
    req = ToolCallRequest("call_nested_1", "nested_tool", nested_args)

    step_decision = ModelDecision(
        decision_id="dec_nested",
        step_id=StepId("step_1"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(req,),
    )
    stop_decision = ModelDecision(
        decision_id="dec_stop",
        step_id=StepId("step_2"),
        finish_reason=ModelFinishReason.STOP,
        content="done",
    )

    # 1. Execution WITHOUT storage
    tool_no_storage = NestedPayloadTool()
    model_no_storage = FakeModelAdapter(responses=[step_decision, stop_decision])
    runtime_no_storage = AgentRuntime(model=model_no_storage, tools=[tool_no_storage], storage=None)
    result_no_storage = runtime_no_storage.run(
        task="Test nested without storage",
        namespace_id=NamespaceId("ns_test"),
        run_id=RunId("run_no_storage"),
    )

    # 2. Execution WITH durable SQLite storage
    tool_with_storage = NestedPayloadTool()
    model_with_storage = FakeModelAdapter(responses=[step_decision, stop_decision])
    with SQLiteStorageAdapter(":memory:") as adapter:
        runtime_with_storage = AgentRuntime(
            model=model_with_storage, tools=[tool_with_storage], storage=adapter
        )
        result_with_storage = runtime_with_storage.run(
            task="Test nested with storage",
            namespace_id=NamespaceId("ns_test"),
            run_id=RunId("run_with_storage"),
        )

        # Invocations must be durably recorded in storage
        step_1_id = result_with_storage.step_history[0].step_id
        from memory_aware_agent_harness.storage.tools_repo import list_invocations_for_step

        invocations = list_invocations_for_step(adapter.conn, step_1_id, NamespaceId("ns_test"))
        assert len(invocations) == 1
        assert invocations[0].arguments_json == '{"n":{"a":1,"items":[10,20]},"tag":"test"}'

    # Compare execution outcomes
    assert result_no_storage.status == result_with_storage.status
    assert result_no_storage.step_count == result_with_storage.step_count

    step_no_st = result_no_storage.step_history[0].tool_results[0]
    step_with_st = result_with_storage.step_history[0].tool_results[0]

    assert step_no_st.status == ToolCallStatus.COMPLETED
    assert step_with_st.status == ToolCallStatus.COMPLETED
    assert step_no_st.result == step_with_st.result

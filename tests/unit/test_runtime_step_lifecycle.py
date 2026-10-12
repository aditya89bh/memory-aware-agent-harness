"""Unit tests for the complete 8-phase step lifecycle sequence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    MemoryId,
    MemoryKind,
    MemoryRecord,
    MemoryState,
    MemoryStoreProtocol,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    SideEffectType,
    TokenUsage,
    ToolCallContext,
    ToolCallRequest,
    ToolProtocol,
)
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime


class DummyTool(ToolProtocol):
    """Test tool implementing ToolProtocol."""

    def __init__(self, name: str = "echo_tool", should_raise: bool = False) -> None:
        self.name = name
        self.description = "A test tool"
        self.side_effect = SideEffectType.READ_ONLY
        self.should_raise = should_raise
        self.invocations: list[tuple[Mapping[str, Any], ToolCallContext]] = []

    def get_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        self.invocations.append((arguments, context))
        if self.should_raise:
            raise RuntimeError("Tool failed internally")
        return {"echo": str(arguments.get("text", ""))}


class DummyMemoryStore(MemoryStoreProtocol):
    """Test memory store implementing MemoryStoreProtocol."""

    def __init__(self, records: Sequence[MemoryRecord] = ()) -> None:
        self._records = list(records)

    def put(self, record: MemoryRecord) -> None:
        self._records.append(record)

    def get(self, memory_id: MemoryId, namespace_id: NamespaceId) -> MemoryRecord | None:
        for r in self._records:
            if r.id == memory_id and r.namespace_id == namespace_id:
                return r
        return None

    def search(
        self,
        namespace_id: NamespaceId,
        query: str,
        kinds: Sequence[MemoryKind],
        limit: int,
    ) -> Sequence[MemoryRecord]:
        return [r for r in self._records if r.namespace_id == namespace_id][:limit]

    def tombstone(self, memory_id: MemoryId, namespace_id: NamespaceId, reason: str) -> None:
        pass

    def list_active(self, namespace_id: NamespaceId, kind: MemoryKind) -> Sequence[MemoryRecord]:
        return [r for r in self._records if r.kind == kind and r.state == MemoryState.ACTIVE]


def test_full_8_phase_sequence_without_tools() -> None:
    model = FakeModelAdapter()
    runtime = AgentRuntime(model=model)

    result = runtime.run("Execute full lifecycle", namespace_id=NamespaceId("ns_life"))
    assert result.status.value == "completed"

    # Verify model was called with ContextManifest
    assert model.call_count == 1
    manifest = model.calls[0][0]
    assert manifest.compiled_prompt == "Execute full lifecycle"


def test_full_8_phase_sequence_with_tool_call() -> None:
    tool = DummyTool(name="search_engine")
    model = FakeModelAdapter.create_tool_calling_adapter(
        tool_name="search_engine",
        arguments={"text": "quantum computing"},
    )

    runtime = AgentRuntime(model=model, tools=[tool])
    result = runtime.run("Research topic", namespace_id=NamespaceId("ns_life"))

    assert result.status.value == "completed"
    assert result.step_count == 2
    assert len(tool.invocations) == 1
    assert tool.invocations[0][0] == {"text": "quantum computing"}


def test_tool_execution_exception_handled() -> None:
    crashing_tool = DummyTool(name="crash_tool", should_raise=True)
    step1_dec = ModelDecision(
        decision_id="dec_crash_tool",
        step_id="step_placeholder",  # type: ignore[arg-type]
        finish_reason=ModelFinishReason.TOOL_CALL,
        content="Invoking crash tool",
        tool_calls=(
            ToolCallRequest(
                call_id="call_c1",
                tool_name="crash_tool",
                arguments={},
            ),
        ),
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    step2_dec = ModelDecision(
        decision_id="dec_finish",
        step_id="step_placeholder",  # type: ignore[arg-type]
        finish_reason=ModelFinishReason.STOP,
        content="Handled tool crash gracefully",
        token_usage=TokenUsage(prompt_tokens=15, completion_tokens=5, total_tokens=20),
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec])

    runtime = AgentRuntime(model=model, tools=[crashing_tool])
    result = runtime.run("Test crashing tool", namespace_id=NamespaceId("ns_life"))

    assert result.status.value == "completed"
    assert result.step_count == 2


def test_unregistered_tool_call_handled() -> None:
    step1_dec = ModelDecision(
        decision_id="dec_missing_tool",
        step_id="step_placeholder",  # type: ignore[arg-type]
        finish_reason=ModelFinishReason.TOOL_CALL,
        content="Invoking missing tool",
        tool_calls=(
            ToolCallRequest(
                call_id="call_m1",
                tool_name="unregistered_tool",
                arguments={},
            ),
        ),
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    step2_dec = ModelDecision(
        decision_id="dec_finish_2",
        step_id="step_placeholder",  # type: ignore[arg-type]
        finish_reason=ModelFinishReason.STOP,
        content="Handled missing tool",
        token_usage=TokenUsage(prompt_tokens=15, completion_tokens=5, total_tokens=20),
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec])

    runtime = AgentRuntime(model=model, tools=[])
    result = runtime.run("Test unregistered tool", namespace_id=NamespaceId("ns_life"))

    assert result.status.value == "completed"
    assert result.step_count == 2


def test_retrieval_phase_populates_context_manifest() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    sample_mem = MemoryRecord(
        id=MemoryId("mem_ret_01"),
        namespace_id=NamespaceId("ns_ret"),
        kind=MemoryKind.SEMANTIC,
        content="User prefers Python 3.11 with strict typing",
        state=MemoryState.ACTIVE,
        confidence=0.95,
        salience=0.8,
        created_at=now,
        updated_at=now,
    )
    store = DummyMemoryStore(records=[sample_mem])
    model = FakeModelAdapter()

    runtime = AgentRuntime(model=model, memory_store=store)
    result = runtime.run("Write code", namespace_id=NamespaceId("ns_ret"))

    assert result.status.value == "completed"
    assert model.call_count == 1
    manifest = model.calls[0][0]
    assert len(manifest.included_items) == 1
    assert manifest.included_items[0].memory_id == "mem_ret_01"
    assert manifest.included_items[0].content == "User prefers Python 3.11 with strict typing"

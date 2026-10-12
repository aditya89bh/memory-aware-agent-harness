"""End-to-end integration tests for deterministic AgentRuntime loop with FakeModelAdapter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from memory_aware_agent_harness.contracts import (
    CancellationToken,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunBudgets,
    RunId,
    RunStatus,
    SideEffectType,
    StepId,
    TokenUsage,
    ToolCallContext,
    ToolCallRequest,
    ToolCallStatus,
    ToolProtocol,
)
from memory_aware_agent_harness.doubles import DeterministicClock, DeterministicIdGenerator
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.events import EventCollector, RuntimeEventType
from memory_aware_agent_harness.runtime.runner import AgentRuntime


class _CalculatorTool(ToolProtocol):
    """Deterministic calculator tool."""

    def __init__(self) -> None:
        self.name = "calculator"
        self.description = "Perform mathematical operations"
        self.side_effect = SideEffectType.READ_ONLY

    def get_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "op": {"type": "string"},
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["op", "a", "b"],
        }

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        op = str(arguments.get("op", "add"))
        a = float(arguments.get("a", 0))
        b = float(arguments.get("b", 0))
        if op == "add":
            return {"result": a + b}
        elif op == "multiply":
            return {"result": a * b}
        raise ValueError(f"Unsupported operation: {op}")


class _FlakyTool(ToolProtocol):
    """Tool that can simulate transient failure and subsequent recovery."""

    def __init__(self) -> None:
        self.name = "flaky_tool"
        self.description = "Simulates failure or success based on arguments"
        self.side_effect = SideEffectType.IDEMPOTENT_WRITE
        self.call_count = 0

    def get_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {"should_fail": {"type": "boolean"}},
        }

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        self.call_count += 1
        if arguments.get("should_fail", False):
            raise RuntimeError("Upstream service unavailable")
        return {"status": "success", "attempt": self.call_count}


class _LookupTool(ToolProtocol):
    """Lookup tool for multi-tool parallel tests."""

    def __init__(self) -> None:
        self.name = "lookup"
        self.description = "Look up data by entity key"
        self.side_effect = SideEffectType.READ_ONLY

    def get_schema(self) -> Mapping[str, Any]:
        return {"type": "object", "properties": {"key": {"type": "string"}}}

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        key = str(arguments.get("key", ""))
        records = {
            "user_1": {"name": "Alice", "role": "admin"},
            "user_2": {"name": "Bob", "role": "engineer"},
        }
        return {"record": records.get(key, {"name": "unknown"})}


def test_e2e_multi_step_tool_use_and_completion() -> None:
    """End-to-end multi-step run: model invokes tool in step 1,
    receives result and stops in step 2.
    """
    clock = DeterministicClock()
    id_gen = DeterministicIdGenerator(prefix="e2e_run1")
    collector = EventCollector()
    calc = _CalculatorTool()

    step1_dec = ModelDecision(
        decision_id="dec_1",
        step_id=StepId("s_1"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(
            ToolCallRequest(
                call_id="call_calc",
                tool_name="calculator",
                arguments={"op": "multiply", "a": 6, "b": 7},
            ),
        ),
        token_usage=TokenUsage(prompt_tokens=25, completion_tokens=15, total_tokens=40),
    )
    step2_dec = ModelDecision(
        decision_id="dec_2",
        step_id=StepId("s_2"),
        finish_reason=ModelFinishReason.STOP,
        content="The calculated answer is 42.",
        token_usage=TokenUsage(prompt_tokens=45, completion_tokens=10, total_tokens=55),
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec])

    runtime = AgentRuntime(
        model=model,
        tools=[calc],
        clock=clock,
        id_generator=id_gen,
        event_listeners=[collector],
    )

    result = runtime.run("What is 6 * 7?", namespace_id=NamespaceId("ns_math"))

    assert result.status == RunStatus.COMPLETED
    assert result.step_count == 2
    assert result.final_output == "The calculated answer is 42."
    assert result.error is None
    assert result.failure_details is None
    assert result.total_tokens_consumed == 95

    # Event audit trail confirms tool execution and completion
    tool_comp = collector.events_of_type(RuntimeEventType.TOOL_COMPLETED)
    assert len(tool_comp) == 1
    assert tool_comp[0].payload["tool_name"] == "calculator"
    assert tool_comp[0].payload["status"] == "completed"

    step_comp = collector.events_of_type(RuntimeEventType.STEP_COMPLETED)
    assert len(step_comp) == 2


def test_e2e_tool_failure_and_recovery() -> None:
    """Model observes tool failure in step 1, retries successfully in step 2, stops in step 3."""
    flaky = _FlakyTool()
    collector = EventCollector()

    step1_dec = ModelDecision(
        decision_id="dec_1",
        step_id=StepId("s_1"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(
            ToolCallRequest(
                call_id="call_fail",
                tool_name="flaky_tool",
                arguments={"should_fail": True},
            ),
        ),
        token_usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
    )
    step2_dec = ModelDecision(
        decision_id="dec_2",
        step_id=StepId("s_2"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(
            ToolCallRequest(
                call_id="call_retry",
                tool_name="flaky_tool",
                arguments={"should_fail": False},
            ),
        ),
        token_usage=TokenUsage(prompt_tokens=35, completion_tokens=10, total_tokens=45),
    )
    step3_dec = ModelDecision(
        decision_id="dec_3",
        step_id=StepId("s_3"),
        finish_reason=ModelFinishReason.STOP,
        content="Successfully recovered on retry.",
        token_usage=TokenUsage(prompt_tokens=50, completion_tokens=10, total_tokens=60),
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec, step3_dec])

    runtime = AgentRuntime(model=model, tools=[flaky], event_listeners=[collector])
    result = runtime.run("Execute resilient task", namespace_id=NamespaceId("ns_resilience"))

    assert result.status == RunStatus.COMPLETED
    assert result.step_count == 3
    assert flaky.call_count == 2
    assert result.final_output == "Successfully recovered on retry."

    # Verify first tool failed with outcome_unknown (P1-M2 mutating tool failure)
    # and second succeeded via event audit trail
    tool_events = collector.events_of_type(RuntimeEventType.TOOL_COMPLETED)
    assert len(tool_events) == 2
    assert tool_events[0].payload["status"] == "outcome_unknown"
    assert tool_events[1].payload["status"] == "completed"
    assert len(result.tool_results) == 2
    assert result.tool_results[0].status == ToolCallStatus.OUTCOME_UNKNOWN
    assert result.tool_results[1].status == ToolCallStatus.COMPLETED


def test_e2e_multi_tool_single_step() -> None:
    """Model executes multiple tool calls within a single step."""
    lookup = _LookupTool()
    collector = EventCollector()

    step1_dec = ModelDecision(
        decision_id="dec_1",
        step_id=StepId("s_1"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(
            ToolCallRequest(call_id="c_u1", tool_name="lookup", arguments={"key": "user_1"}),
            ToolCallRequest(call_id="c_u2", tool_name="lookup", arguments={"key": "user_2"}),
        ),
        token_usage=TokenUsage(prompt_tokens=30, completion_tokens=20, total_tokens=50),
    )
    step2_dec = ModelDecision(
        decision_id="dec_2",
        step_id=StepId("s_2"),
        finish_reason=ModelFinishReason.STOP,
        content="Alice is admin, Bob is engineer.",
        token_usage=TokenUsage(prompt_tokens=60, completion_tokens=10, total_tokens=70),
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec])

    runtime = AgentRuntime(model=model, tools=[lookup], event_listeners=[collector])
    result = runtime.run("Fetch team members", namespace_id=NamespaceId("ns_team"))

    assert result.status == RunStatus.COMPLETED
    assert result.step_count == 2

    tool_events = collector.events_of_type(RuntimeEventType.TOOL_COMPLETED)
    assert len(tool_events) == 2
    assert tool_events[0].payload["tool_name"] == "lookup"
    assert tool_events[0].payload["status"] == "completed"
    assert tool_events[1].payload["tool_name"] == "lookup"
    assert tool_events[1].payload["status"] == "completed"


def test_e2e_token_budget_exhaustion_multi_turn() -> None:
    """Cumulative token consumption across steps enforces max_tokens budget ceiling."""
    decisions = [
        ModelDecision(
            decision_id=f"dec_{i}",
            step_id=StepId(f"s_{i}"),
            finish_reason=ModelFinishReason.LENGTH,
            content=f"Step {i} output",
            token_usage=TokenUsage(prompt_tokens=30, completion_tokens=30, total_tokens=60),
        )
        for i in range(5)
    ]
    model = FakeModelAdapter(responses=decisions)

    budgets = RunBudgets(max_tokens=100, max_steps=10)
    runtime = AgentRuntime(model=model, budgets=budgets)

    result = runtime.run("Token heavy task", namespace_id=NamespaceId("ns_tokens"))

    assert result.status == RunStatus.BUDGET_EXHAUSTED
    assert result.step_count == 2
    assert result.total_tokens_consumed == 120
    assert result.failure_details is not None
    assert result.failure_details.error_code == "BUDGET_EXCEEDED"
    assert "token budget exceeded" in result.failure_details.message.lower()


def test_e2e_time_budget_exhaustion() -> None:
    """Clock advancement past max_time_seconds halts execution with BUDGET_EXHAUSTED."""
    clock = DeterministicClock()

    class _TimeAdvancingModel(FakeModelAdapter):
        def decide(self, manifest: Any, tools: Any, context: Any) -> ModelDecision:
            clock.advance(timedelta(seconds=40))
            return ModelDecision(
                decision_id="dec_slow",
                step_id=context.step_id,
                finish_reason=ModelFinishReason.LENGTH,
                content="Slow thinking",
            )

    model = _TimeAdvancingModel()
    budgets = RunBudgets(max_time_seconds=50.0, max_steps=10)
    runtime = AgentRuntime(model=model, clock=clock, budgets=budgets)

    result = runtime.run("Time-constrained task", namespace_id=NamespaceId("ns_time"))

    assert result.status == RunStatus.BUDGET_EXHAUSTED
    assert result.step_count == 2
    assert result.failure_details is not None
    assert "time budget exceeded" in result.failure_details.message.lower()


def test_e2e_cooperative_cancellation_mid_execution() -> None:
    """Cancellation requested after step 1 halts subsequent steps cleanly."""
    token = CancellationToken()

    class _CancellingOnStep1Model(FakeModelAdapter):
        def decide(self, manifest: Any, tools: Any, context: Any) -> ModelDecision:
            token.cancel("User cancelled after step 1")
            return ModelDecision(
                decision_id="dec_1",
                step_id=context.step_id,
                finish_reason=ModelFinishReason.LENGTH,
                content="Finished step 1",
            )

    model = _CancellingOnStep1Model()
    runtime = AgentRuntime(model=model)

    result = runtime.run(
        "Interruptible task",
        namespace_id=NamespaceId("ns_can"),
        cancellation_token=token,
    )

    assert result.status == RunStatus.CANCELLED
    assert result.step_count == 1
    assert result.failure_details is not None
    assert result.failure_details.error_code == "EXECUTION_CANCELLED"
    assert "User cancelled after step 1" in result.failure_details.message


def test_e2e_diagnostic_events_integrity() -> None:
    """Event collector receives strictly ordered, unbroken sequence of lifecycle events."""
    collector = EventCollector()
    clock = DeterministicClock()
    id_gen = DeterministicIdGenerator(prefix="audit")
    calc = _CalculatorTool()

    step1_dec = ModelDecision(
        decision_id="dec_1",
        step_id=StepId("s_1"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(
            ToolCallRequest(
                call_id="c_add",
                tool_name="calculator",
                arguments={"op": "add", "a": 10, "b": 20},
            ),
        ),
    )
    step2_dec = ModelDecision(
        decision_id="dec_2",
        step_id=StepId("s_2"),
        finish_reason=ModelFinishReason.STOP,
        content="Answer is 30",
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec])

    runtime = AgentRuntime(
        model=model,
        tools=[calc],
        clock=clock,
        id_generator=id_gen,
        event_listeners=[collector],
    )

    result = runtime.run(
        "Compute sum",
        namespace_id=NamespaceId("ns_audit"),
        run_id=RunId("run_audit_42"),
    )
    assert result.status == RunStatus.COMPLETED

    events = collector.events
    assert len(events) > 10

    # Ensure first event is RUN_STARTED and last is RUN_TERMINATED
    assert events[0].event_type == RuntimeEventType.RUN_STARTED
    assert events[0].run_id == "run_audit_42"
    assert events[-1].event_type == RuntimeEventType.RUN_TERMINATED
    assert events[-1].run_id == "run_audit_42"

    # Strictly monotonic sequence numbers with no skips or duplicates
    seqs = [e.sequence_number for e in events]
    assert seqs == list(range(len(events)))

    # Every event matches run_id
    assert all(e.run_id == "run_audit_42" for e in events)

    # Tool events correctly capture tool name and status
    tool_inv_events = collector.events_of_type(RuntimeEventType.TOOL_INVOKED)
    assert len(tool_inv_events) == 1
    assert tool_inv_events[0].payload["tool_name"] == "calculator"

    tool_comp_events = collector.events_of_type(RuntimeEventType.TOOL_COMPLETED)
    assert len(tool_comp_events) == 1
    assert tool_comp_events[0].payload["status"] == "completed"

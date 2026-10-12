"""Unit tests for cooperative cancellation checkpoints throughout execution loop."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from memory_aware_agent_harness.contracts import (
    CancellationToken,
    ContextManifest,
    ExecutionCancelledError,
    ModelCallContext,
    ModelDecision,
    ModelFinishReason,
    ModelPort,
    NamespaceId,
    PolicyViolationError,
    RunStatus,
    SideEffectType,
    StepId,
    ToolCallContext,
    ToolCallRequest,
    ToolProtocol,
)
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime


class _CancellingTool(ToolProtocol):
    """Tool that triggers token cancellation or raises ExecutionCancelledError during execution."""

    def __init__(self, name: str = "cancelling_tool") -> None:
        self.name = name
        self.description = "Cancelling tool"
        self.side_effect = SideEffectType.READ_ONLY

    def get_schema(self) -> Mapping[str, Any]:
        return {"name": self.name}

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        if context.cancellation_token is not None:
            context.cancellation_token.cancel("Cancelled by tool invocation")
            context.cancellation_token.check_cancellation()
        raise ExecutionCancelledError("Cancelled in tool")


class _PolicyViolatingTool(ToolProtocol):
    """Tool that raises PolicyViolationError during execution."""

    def __init__(self, name: str = "violating_tool") -> None:
        self.name = name
        self.description = "Policy violating tool"
        self.side_effect = SideEffectType.NON_IDEMPOTENT_WRITE

    def get_schema(self) -> Mapping[str, Any]:
        return {"name": self.name}

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        raise PolicyViolationError(
            "Access to forbidden resource denied", policy_name="sandbox_boundary"
        )


class _CancellingModel(ModelPort):
    """Model adapter that cancels token during decide phase."""

    def __init__(self, cancel_before_decision: bool = True) -> None:
        self.cancel_before_decision = cancel_before_decision

    def decide(
        self,
        manifest: ContextManifest,
        tools: Sequence[Mapping[str, Any]],
        context: ModelCallContext,
    ) -> ModelDecision:
        if self.cancel_before_decision and context.cancellation_token is not None:
            context.cancellation_token.cancel("Cancelled inside model decide")
            context.cancellation_token.check_cancellation()
        return ModelDecision(
            decision_id="dec_cancel",
            step_id=context.step_id,
            finish_reason=ModelFinishReason.STOP,
            content="Done",
        )


def test_cancellation_pre_run() -> None:
    """Pre-run cancellation immediately terminates without executing any steps."""
    token = CancellationToken()
    token.cancel(reason="Aborted before run started")

    runtime = AgentRuntime(model=FakeModelAdapter())
    result = runtime.run("Task", namespace_id=NamespaceId("ns"), cancellation_token=token)

    assert result.status == RunStatus.CANCELLED
    assert result.step_count == 0
    assert result.failure_details is not None
    assert result.failure_details.error_code == "EXECUTION_CANCELLED"


def test_cancellation_during_model_decide() -> None:
    """Cancellation during model decision aborts step and transitions run to CANCELLED."""
    token = CancellationToken()
    model = _CancellingModel(cancel_before_decision=True)

    runtime = AgentRuntime(model=model)
    result = runtime.run("Task", namespace_id=NamespaceId("ns"), cancellation_token=token)

    assert result.status == RunStatus.CANCELLED
    assert result.step_count == 1
    assert result.failure_details is not None
    assert result.failure_details.error_type == "ExecutionCancelledError"
    assert "Cancelled inside model decide" in result.failure_details.message


def test_cancellation_during_tool_execution() -> None:
    """Cancellation during tool execution propagates and aborts step and run."""
    token = CancellationToken()
    cancelling_tool = _CancellingTool(name="stop_tool")

    decision = ModelDecision(
        decision_id="dec_tool",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(ToolCallRequest(call_id="call_1", tool_name="stop_tool", arguments={}),),
    )
    model = FakeModelAdapter(responses=[decision])

    runtime = AgentRuntime(model=model, tools=[cancelling_tool])
    result = runtime.run("Task", namespace_id=NamespaceId("ns"), cancellation_token=token)

    assert result.status == RunStatus.CANCELLED
    assert result.step_count == 1
    assert result.failure_details is not None
    assert result.failure_details.error_code == "EXECUTION_CANCELLED"


def test_policy_violation_in_tool_aborts_run() -> None:
    """PolicyViolationError raised by a tool aborts step and transitions run to ABORTED."""
    violating_tool = _PolicyViolatingTool(name="bad_tool")

    decision = ModelDecision(
        decision_id="dec_tool",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(ToolCallRequest(call_id="call_1", tool_name="bad_tool", arguments={}),),
    )
    model = FakeModelAdapter(responses=[decision])

    runtime = AgentRuntime(model=model, tools=[violating_tool])
    result = runtime.run("Task", namespace_id=NamespaceId("ns"))

    assert result.status == RunStatus.ABORTED
    assert result.step_count == 1
    assert result.failure_details is not None
    assert result.failure_details.error_type == "PolicyViolationError"
    assert result.failure_details.error_code == "POLICY_VIOLATION"
    assert "Access to forbidden resource denied" in result.failure_details.message


def test_cancellation_between_tool_calls() -> None:
    """Cancellation after first tool call prevents execution of second tool call."""
    token = CancellationToken()

    class _FirstTool(ToolProtocol):
        called = False

        def __init__(self) -> None:
            self.name = "tool_1"
            self.description = "First tool"
            self.side_effect = SideEffectType.READ_ONLY

        def get_schema(self) -> Mapping[str, Any]:
            return {"name": "tool_1"}

        def execute(
            self, arguments: Mapping[str, Any], context: ToolCallContext
        ) -> Mapping[str, Any]:
            _FirstTool.called = True
            # Cancel token during first tool call
            if context.cancellation_token is not None:
                context.cancellation_token.cancel("Cancelled after tool 1 finished")
            return {"status": "ok"}

    class _SecondTool(ToolProtocol):
        called = False

        def __init__(self) -> None:
            self.name = "tool_2"
            self.description = "Second tool"
            self.side_effect = SideEffectType.READ_ONLY

        def get_schema(self) -> Mapping[str, Any]:
            return {"name": "tool_2"}

        def execute(
            self, arguments: Mapping[str, Any], context: ToolCallContext
        ) -> Mapping[str, Any]:
            _SecondTool.called = True
            return {"status": "never_run"}

    t1 = _FirstTool()
    t2 = _SecondTool()

    decision = ModelDecision(
        decision_id="dec_multi",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(
            ToolCallRequest(call_id="c1", tool_name="tool_1", arguments={}),
            ToolCallRequest(call_id="c2", tool_name="tool_2", arguments={}),
        ),
    )
    model = FakeModelAdapter(responses=[decision])

    runtime = AgentRuntime(model=model, tools=[t1, t2])
    result = runtime.run(
        "Multi tool task",
        namespace_id=NamespaceId("ns"),
        cancellation_token=token,
    )

    assert result.status == RunStatus.CANCELLED
    assert _FirstTool.called is True
    assert _SecondTool.called is False
    assert result.failure_details is not None
    assert result.failure_details.error_code == "EXECUTION_CANCELLED"

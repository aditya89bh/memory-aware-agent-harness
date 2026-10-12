"""Integration tests for model observation feedback and bounded execution history (P1-H1).

Validates that:
1. Subsequent model requests receive prior model decisions and tool observations.
2. An observation-dependent model dynamically branches based on tool outcomes.
3. Execution prompt history remains strictly bounded by window limits.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from memory_aware_agent_harness.contracts import (
    ContextManifest,
    ModelCallContext,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunBudgets,
    RunStatus,
    SideEffectType,
    TokenUsage,
    ToolCallContext,
    ToolCallRequest,
    ToolProtocol,
)
from memory_aware_agent_harness.doubles import DeterministicClock, DeterministicIdGenerator
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime, compile_execution_prompt


class _SensorTool(ToolProtocol):
    """Controllable sensor tool returning configurable readings."""

    def __init__(self, reading: float | None = 25.0, should_fail: bool = False) -> None:
        self.name = "sensor"
        self.description = "Read environmental temperature sensor"
        self.side_effect = SideEffectType.READ_ONLY
        self.reading = reading
        self.should_fail = should_fail

    def get_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {"unit": {"type": "string"}},
        }

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        if self.should_fail:
            raise RuntimeError("Sensor hardware disconnected")
        return {"temperature": self.reading, "unit": arguments.get("unit", "celsius")}


def test_observation_feedback_dynamic_decision_branching() -> None:
    """P1-H1: Model consumes tool observation in prompt and branches decision dynamically."""
    sensor = _SensorTool(reading=35.5)

    def _dynamic_model_handler(
        manifest: ContextManifest,
        tools_schema: Sequence[Mapping[str, Any]],
        context: ModelCallContext,
    ) -> ModelDecision:
        prompt = manifest.compiled_prompt or ""
        # Check if we have an observation from the sensor tool
        if "Observation (sensor" in prompt:
            # Parse observation from prompt
            temp: float | None = None
            for line in prompt.splitlines():
                if "Observation (sensor" in line:
                    obs_payload = json.loads(line.split(":", 1)[-1].strip())
                    temp = float(obs_payload.get("temperature", 0.0))
            if temp is not None and temp > 30.0:
                return ModelDecision(
                    decision_id="dec_alarm",
                    step_id=context.step_id,
                    finish_reason=ModelFinishReason.STOP,
                    content=f"HIGH TEMPERATURE ALERT: {temp}C exceeds threshold",
                    token_usage=TokenUsage(prompt_tokens=25, completion_tokens=15, total_tokens=40),
                )
            else:
                return ModelDecision(
                    decision_id="dec_normal",
                    step_id=context.step_id,
                    finish_reason=ModelFinishReason.STOP,
                    content=f"Temperature normal: {temp}C",
                    token_usage=TokenUsage(prompt_tokens=25, completion_tokens=15, total_tokens=40),
                )

        # Step 1: Call sensor tool
        return ModelDecision(
            decision_id="dec_call",
            step_id=context.step_id,
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_01",
                    tool_name="sensor",
                    arguments={"unit": "celsius"},
                ),
            ),
            token_usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        )

    model = FakeModelAdapter(default_handler=_dynamic_model_handler)
    runtime = AgentRuntime(
        model=model,
        tools=[sensor],
        clock=DeterministicClock(),
        id_generator=DeterministicIdGenerator(prefix="test"),
    )

    # 1. Run with high temperature (35.5) -> should branch to HIGH TEMPERATURE ALERT
    res1 = runtime.run("Monitor climate", namespace_id=NamespaceId("ns_sensor"))
    assert res1.status == RunStatus.COMPLETED
    assert res1.step_count == 2
    assert res1.final_output == "HIGH TEMPERATURE ALERT: 35.5C exceeds threshold"

    # 2. Run with normal temperature (22.0) -> should branch to Temperature normal
    sensor.reading = 22.0
    model = FakeModelAdapter(default_handler=_dynamic_model_handler)
    runtime = AgentRuntime(model=model, tools=[sensor])
    res2 = runtime.run("Monitor climate", namespace_id=NamespaceId("ns_sensor"))
    assert res2.status == RunStatus.COMPLETED
    assert res2.step_count == 2
    assert res2.final_output == "Temperature normal: 22.0C"


def test_observation_feedback_on_tool_failure_recovery() -> None:
    """P1-H1: Model consumes tool failure observation and branches to recovery."""
    failed_sensor = _SensorTool(should_fail=True)

    def _recovery_handler(
        manifest: ContextManifest,
        tools_schema: Sequence[Mapping[str, Any]],
        context: ModelCallContext,
    ) -> ModelDecision:
        prompt = manifest.compiled_prompt or ""
        if "Observation (sensor, failed)" in prompt:
            return ModelDecision(
                decision_id="dec_fallback",
                step_id=context.step_id,
                finish_reason=ModelFinishReason.STOP,
                content="Sensor failed; activated backup cooling protocol.",
                token_usage=TokenUsage(prompt_tokens=30, completion_tokens=15, total_tokens=45),
            )
        return ModelDecision(
            decision_id="dec_call",
            step_id=context.step_id,
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="c_err",
                    tool_name="sensor",
                    arguments={},
                ),
            ),
            token_usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        )

    runtime = AgentRuntime(
        model=FakeModelAdapter(default_handler=_recovery_handler),
        tools=[failed_sensor],
    )
    result = runtime.run("Check system health", namespace_id=NamespaceId("ns_recovery"))
    assert result.status == RunStatus.COMPLETED
    assert result.step_count == 2
    assert result.final_output == "Sensor failed; activated backup cooling protocol."


def test_bounded_execution_history_prevents_prompt_accumulation() -> None:
    """P1-H1: Bounded history window strictly caps prompt accumulation across many steps."""
    step_prompts: list[str] = []

    def _recorder_handler(
        manifest: ContextManifest,
        tools_schema: Sequence[Mapping[str, Any]],
        context: ModelCallContext,
    ) -> ModelDecision:
        step_prompts.append(manifest.compiled_prompt or "")
        # Run 8 steps, stopping on step 8
        if len(step_prompts) >= 8:
            return ModelDecision(
                decision_id=f"dec_{context.step_id}",
                step_id=context.step_id,
                finish_reason=ModelFinishReason.STOP,
                content="Finished 8-step journey",
                token_usage=TokenUsage(prompt_tokens=15, completion_tokens=5, total_tokens=20),
            )
        return ModelDecision(
            decision_id=f"dec_{context.step_id}",
            step_id=context.step_id,
            finish_reason=ModelFinishReason.LENGTH,
            content=f"Intermediate step {len(step_prompts)}",
            token_usage=TokenUsage(prompt_tokens=15, completion_tokens=5, total_tokens=20),
        )

    runtime = AgentRuntime(
        model=FakeModelAdapter(default_handler=_recorder_handler),
        budgets=RunBudgets(max_steps=10),
    )
    res = runtime.run("Long sequence task", namespace_id=NamespaceId("ns_bounded"))
    assert res.status == RunStatus.COMPLETED
    assert res.step_count == 8

    # Step 1 prompt only has task
    assert "--- Execution History ---" not in step_prompts[0]
    assert "[Step 1]" not in step_prompts[0]

    # Step 8 prompt has history, bounded
    assert "--- Execution History ---" in step_prompts[7]
    assert "[Step 1]" in step_prompts[7]
    assert "[Step 7]" in step_prompts[7]

    # Verify compile_execution_prompt window limit
    test_history = tuple(res.step_history)
    prompt_win3 = compile_execution_prompt("My task", test_history, max_history_steps=3)
    # With max_history_steps=3, should only contain steps 6, 7, 8
    assert "[Step 8]" in prompt_win3
    assert "[Step 7]" in prompt_win3
    assert "[Step 6]" in prompt_win3
    assert "[Step 1]" not in prompt_win3
    assert "[Step 2]" not in prompt_win3

"""Unit tests for FakeModelAdapter."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from memory_aware_agent_harness.contracts import (
    CancellationToken,
    ContextManifest,
    ExecutionCancelledError,
    ModelCallContext,
    ModelDecision,
    ModelFinishReason,
    ModelPort,
    NamespaceId,
    RunId,
    StepId,
    TokenUsage,
)
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter


def _create_sample_manifest(prompt: str = "Test prompt") -> ContextManifest:
    return ContextManifest(
        manifest_id="mf_001",
        step_id=StepId("step_001"),
        included_items=(),
        excluded_item_ids={},
        total_tokens=len(prompt.split()),
        budget_limit=1000,
        compiled_prompt=prompt,
    )


def _create_sample_context(
    step_id: str = "step_001",
    cancelled: bool = False,
) -> ModelCallContext:
    token = CancellationToken(cancelled=cancelled)
    return ModelCallContext(
        run_id=RunId("run_001"),
        step_id=StepId(step_id),
        namespace_id=NamespaceId("ns_001"),
        budget_remaining_tokens=5000,
        cancellation_token=token,
    )


def test_fake_model_adapter_implements_protocol() -> None:
    adapter = FakeModelAdapter()
    assert isinstance(adapter, ModelPort)
    assert adapter.call_count == 0
    assert len(adapter.calls) == 0


def test_fake_model_adapter_fallback_decision() -> None:
    adapter = FakeModelAdapter()
    manifest = _create_sample_manifest("Summarize the quarterly system reports")
    context = _create_sample_context()

    decision = adapter.decide(manifest, [], context)

    assert decision.step_id == "step_001"
    assert decision.finish_reason == ModelFinishReason.STOP
    assert "Deterministic completion" in (decision.content or "")
    assert decision.token_usage is not None
    assert decision.tokens_used == decision.token_usage.total_tokens
    assert adapter.call_count == 1
    assert adapter.calls[0][0] == manifest


def test_fake_model_adapter_scripted_queue() -> None:
    scripted_1 = ModelDecision(
        decision_id="script_1",
        step_id=StepId("step_001"),
        finish_reason=ModelFinishReason.STOP,
        content="First scripted response",
    )
    scripted_2 = ModelDecision(
        decision_id="script_2",
        step_id=StepId("placeholder_step"),
        finish_reason=ModelFinishReason.STOP,
        content="Second scripted response",
    )

    adapter = FakeModelAdapter(responses=[scripted_1])
    adapter.queue_response(scripted_2)

    manifest = _create_sample_manifest()
    ctx1 = _create_sample_context("step_001")
    ctx2 = _create_sample_context("step_002")

    dec1 = adapter.decide(manifest, [], ctx1)
    assert dec1.decision_id == "script_1"
    assert dec1.content == "First scripted response"

    dec2 = adapter.decide(manifest, [], ctx2)
    assert dec2.decision_id == "script_2"
    assert dec2.step_id == "step_002"  # Adapted to context.step_id
    assert dec2.content == "Second scripted response"

    # Next call falls back to default generator
    ctx3 = _create_sample_context("step_003")
    dec3 = adapter.decide(manifest, [], ctx3)
    assert dec3.step_id == "step_003"
    assert adapter.call_count == 3


def test_fake_model_adapter_custom_handler() -> None:
    def custom_handler(
        m: ContextManifest,
        tools: Sequence[object],
        ctx: ModelCallContext,
    ) -> ModelDecision:
        return ModelDecision(
            decision_id="custom_id",
            step_id=ctx.step_id,
            finish_reason=ModelFinishReason.STOP,
            content=f"Custom: {m.compiled_prompt.upper()}",
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    adapter = FakeModelAdapter(default_handler=custom_handler)
    manifest = _create_sample_manifest("hello world")
    context = _create_sample_context()

    decision = adapter.decide(manifest, [], context)
    assert decision.content == "Custom: HELLO WORLD"
    assert decision.tokens_used == 15


def test_fake_model_adapter_simulated_exceptions() -> None:
    # Raise on call index 1
    adapter = FakeModelAdapter(
        exception_to_raise=TimeoutError("Model API timed out"),
        raise_on_call_index=1,
    )
    manifest = _create_sample_manifest()
    ctx1 = _create_sample_context("step_001")
    ctx2 = _create_sample_context("step_002")

    # Call 0 succeeds
    dec1 = adapter.decide(manifest, [], ctx1)
    assert dec1.step_id == "step_001"

    # Call 1 raises
    with pytest.raises(TimeoutError, match="Model API timed out"):
        adapter.decide(manifest, [], ctx2)


def test_fake_model_adapter_checks_cancellation() -> None:
    adapter = FakeModelAdapter()
    manifest = _create_sample_manifest()
    context = _create_sample_context(cancelled=True)

    with pytest.raises(ExecutionCancelledError):
        adapter.decide(manifest, [], context)


def test_fake_model_adapter_tool_calling_factory() -> None:
    adapter = FakeModelAdapter.create_tool_calling_adapter(
        tool_name="calculator",
        arguments={"expression": "2 + 2"},
    )
    manifest = _create_sample_manifest()
    ctx1 = _create_sample_context("step_001")
    ctx2 = _create_sample_context("step_002")

    dec1 = adapter.decide(manifest, [], ctx1)
    assert dec1.finish_reason == ModelFinishReason.TOOL_CALL
    assert len(dec1.tool_calls) == 1
    assert dec1.tool_calls[0].tool_name == "calculator"
    assert dec1.tool_calls[0].arguments == {"expression": "2 + 2"}

    dec2 = adapter.decide(manifest, [], ctx2)
    assert dec2.finish_reason == ModelFinishReason.STOP
    assert "Tool execution analyzed" in (dec2.content or "")

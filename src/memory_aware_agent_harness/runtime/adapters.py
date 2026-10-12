"""Deterministic fake model adapter implementing ModelPort for offline execution and testing.

Provides scripted, rule-based, and fallback decision generation with full call
inspection, cancellation checking, and realistic token consumption.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from memory_aware_agent_harness.contracts import (
    ContextManifest,
    ModelCallContext,
    ModelDecision,
    ModelFinishReason,
    ModelPort,
    TokenUsage,
    ToolCallRequest,
)


class FakeModelAdapter(ModelPort):
    """Controllable test double implementing ModelPort for deterministic runs."""

    def __init__(
        self,
        responses: Sequence[ModelDecision] | None = None,
        default_handler: (
            Callable[
                [ContextManifest, Sequence[Mapping[str, Any]], ModelCallContext],
                ModelDecision,
            ]
            | None
        ) = None,
        exception_to_raise: Exception | None = None,
        raise_on_call_index: int | None = None,
    ) -> None:
        self._responses: list[ModelDecision] = list(responses) if responses is not None else []
        self._default_handler = default_handler
        self._exception_to_raise = exception_to_raise
        self._raise_on_call_index = raise_on_call_index
        self._calls: list[
            tuple[ContextManifest, tuple[Mapping[str, Any], ...], ModelCallContext]
        ] = []
        self._call_counter = 0

    @property
    def call_count(self) -> int:
        """Total number of decide() invocations."""
        return len(self._calls)

    @property
    def calls(
        self,
    ) -> tuple[tuple[ContextManifest, tuple[Mapping[str, Any], ...], ModelCallContext], ...]:
        """Inspection history of all calls made to this adapter."""
        return tuple(self._calls)

    def queue_response(self, response: ModelDecision) -> None:
        """Queue a scripted ModelDecision to be returned by future decide() calls."""
        self._responses.append(response)

    def decide(
        self,
        manifest: ContextManifest,
        tools_schema: Sequence[Mapping[str, Any]],
        context: ModelCallContext,
    ) -> ModelDecision:
        """Produce a deterministic decision while honoring context constraints."""
        # 1. Enforce live cancellation checks
        context.check_cancellation()

        # 2. Record invocation
        self._calls.append((manifest, tuple(tools_schema), context))
        current_index = self._call_counter
        self._call_counter += 1

        # 3. Simulate configured exception if triggered
        if self._exception_to_raise is not None and (
            self._raise_on_call_index is None or self._raise_on_call_index == current_index
        ):
            raise self._exception_to_raise

        # 4. Return scripted response if available
        if self._responses:
            scripted = self._responses.pop(0)
            # Ensure decision matches current step_id if placeholder
            if scripted.step_id != context.step_id:
                return ModelDecision(
                    decision_id=scripted.decision_id,
                    step_id=context.step_id,
                    finish_reason=scripted.finish_reason,
                    content=scripted.content,
                    tool_calls=scripted.tool_calls,
                    tokens_used=scripted.tokens_used,
                    token_usage=scripted.token_usage,
                    failure_details=scripted.failure_details,
                    raw_response=scripted.raw_response,
                )
            return scripted

        # 5. Delegate to custom handler if provided
        if self._default_handler is not None:
            return self._default_handler(manifest, tools_schema, context)

        # 6. Fallback default deterministic decision
        words = len(manifest.compiled_prompt.split()) if manifest.compiled_prompt else 10
        prompt_tokens = max(1, words)
        completion_tokens = 8
        total_tokens = prompt_tokens + completion_tokens
        token_usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        return ModelDecision(
            decision_id=f"fake_dec_{context.step_id}_{current_index + 1}",
            step_id=context.step_id,
            finish_reason=ModelFinishReason.STOP,
            content=f"Deterministic completion for step '{context.step_id}'",
            token_usage=token_usage,
            tokens_used=total_tokens,
        )

    @classmethod
    def create_tool_calling_adapter(
        cls,
        tool_name: str,
        arguments: Mapping[str, Any],
        follow_up_content: str = "Tool execution analyzed, task complete",
    ) -> FakeModelAdapter:
        """Create a model double that issues a tool call on step 1 and finishes on step 2."""
        step_1_decision = ModelDecision(
            decision_id="tool_call_dec_1",
            step_id="step_placeholder",  # type: ignore[arg-type]
            finish_reason=ModelFinishReason.TOOL_CALL,
            content="Decided to invoke tool",
            tool_calls=(
                ToolCallRequest(
                    call_id="call_001",
                    tool_name=tool_name,
                    arguments=dict(arguments),
                ),
            ),
            token_usage=TokenUsage(prompt_tokens=25, completion_tokens=15, total_tokens=40),
            tokens_used=40,
        )
        step_2_decision = ModelDecision(
            decision_id="final_dec_2",
            step_id="step_placeholder",  # type: ignore[arg-type]
            finish_reason=ModelFinishReason.STOP,
            content=follow_up_content,
            token_usage=TokenUsage(prompt_tokens=30, completion_tokens=10, total_tokens=40),
            tokens_used=40,
        )
        return cls(responses=[step_1_decision, step_2_decision])

"""Unit and behavioral tests for strict budget overruns, deadlines, and side-effects.

Covers P1-H2, P1-M1, and P1-M2.

Validates that:
1. 100x token overruns immediately halt execution and report BUDGET_EXHAUSTED, never COMPLETED.
2. Wall-clock elapsed time budget breaches report BUDGET_EXHAUSTED, never COMPLETED.
3. Tokens are recorded immediately upon model return even when errors occur.
4. Failed mutating tools flag OUTCOME_UNKNOWN and side-effect uncertainty.
5. RunResult provides complete step_history and tool_results invocation summaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from memory_aware_agent_harness.contracts import (
    ModelCallContext,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunBudgets,
    RunStatus,
    SideEffectType,
    StepId,
    TokenUsage,
    ToolCallContext,
    ToolCallRequest,
    ToolCallStatus,
    ToolProtocol,
)
from memory_aware_agent_harness.doubles import DeterministicClock
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime


class _OverrunModel(FakeModelAdapter):
    """Model that consumes an enormous number of tokens in a single step."""

    def __init__(
        self, tokens_to_consume: int, finish_reason: ModelFinishReason = ModelFinishReason.STOP
    ) -> None:
        super().__init__()
        self.tokens_to_consume = tokens_to_consume
        self.finish_reason = finish_reason

    def decide(
        self,
        manifest: Any,
        tools_schema: Any,
        context: ModelCallContext,
    ) -> ModelDecision:
        usage = TokenUsage(
            prompt_tokens=self.tokens_to_consume // 2,
            completion_tokens=self.tokens_to_consume // 2,
            total_tokens=self.tokens_to_consume,
        )
        return ModelDecision(
            decision_id="dec_overrun",
            step_id=context.step_id,
            finish_reason=self.finish_reason,
            content="Attempted completion despite huge token overrun",
            token_usage=usage,
            tokens_used=self.tokens_to_consume,
        )


class _DelayTool(ToolProtocol):
    """Tool that advances a simulated clock during execution."""

    def __init__(self, clock: DeterministicClock, delay_seconds: float) -> None:
        self.name = "delay_tool"
        self.description = "Advances clock during execution"
        self.side_effect = SideEffectType.READ_ONLY
        self.clock = clock
        self.delay_seconds = delay_seconds

    def get_schema(self) -> Mapping[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        self.clock.advance(self.delay_seconds)
        return {"elapsed": self.delay_seconds}


class _FailingMutatingTool(ToolProtocol):
    """Mutating tool that raises an exception during execution."""

    def __init__(self, side_effect: SideEffectType = SideEffectType.NON_IDEMPOTENT_WRITE) -> None:
        self.name = "mutating_writer"
        self.description = "Mutating external service writer"
        self.side_effect = side_effect

    def get_schema(self) -> Mapping[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        raise ConnectionResetError("Connection dropped during external write")


def test_100x_token_budget_overrun_halts_as_budget_exhausted() -> None:
    """P1-H2: 100x token overrun immediately halts run with BUDGET_EXHAUSTED, never COMPLETED."""
    # Budget is 1,000 tokens; model returns 100,000 tokens with STOP
    budget = RunBudgets(max_tokens=1_000)
    model = _OverrunModel(tokens_to_consume=100_000, finish_reason=ModelFinishReason.STOP)
    runtime = AgentRuntime(model=model, budgets=budget)

    result = runtime.run(
        "Task with massive token consumption",
        namespace_id=NamespaceId("ns_overrun"),
    )

    # Invariant: Must report BUDGET_EXHAUSTED, never COMPLETED
    assert result.status == RunStatus.BUDGET_EXHAUSTED
    assert result.step_count == 1
    assert result.total_tokens_consumed == 100_000
    assert result.token_usage is not None
    assert result.token_usage.total_tokens == 100_000
    assert result.failure_details is not None
    assert result.failure_details.error_code == "BUDGET_EXCEEDED"
    assert result.final_output is None


def test_time_budget_overrun_halts_as_budget_exhausted() -> None:
    """P1-H2: Wall-clock elapsed time overrun halts run with BUDGET_EXHAUSTED, never COMPLETED."""
    clock = DeterministicClock()
    # Budget is 5 seconds
    budget = RunBudgets(max_time_seconds=5.0)

    # Tool advances clock by 10 seconds during step 1
    delay_tool = _DelayTool(clock=clock, delay_seconds=10.0)

    step1_dec = ModelDecision(
        decision_id="dec_1",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(ToolCallRequest(call_id="c_1", tool_name="delay_tool", arguments={}),),
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    step2_dec = ModelDecision(
        decision_id="dec_2",
        step_id=StepId("step_1"),
        finish_reason=ModelFinishReason.STOP,
        content="Should never complete because time budget breached",
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec])

    runtime = AgentRuntime(
        model=model,
        tools=[delay_tool],
        clock=clock,
        budgets=budget,
    )
    result = runtime.run("Task with time overrun", namespace_id=NamespaceId("ns_time"))

    assert result.status == RunStatus.BUDGET_EXHAUSTED
    assert result.step_count == 1
    assert result.failure_details is not None
    assert result.failure_details.error_code == "BUDGET_EXCEEDED"


def test_immediate_token_recording_on_model_error() -> None:
    """P1-M1: Tokens consumed by model are immediately recorded even on model ERROR."""
    err_decision = ModelDecision(
        decision_id="dec_err",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.ERROR,
        content="Model content blocked",
        token_usage=TokenUsage(prompt_tokens=150, completion_tokens=50, total_tokens=200),
    )
    model = FakeModelAdapter(responses=[err_decision])
    runtime = AgentRuntime(model=model)
    result = runtime.run("Error task", namespace_id=NamespaceId("ns_err_tokens"))

    assert result.status == RunStatus.FAILED
    assert result.total_tokens_consumed == 200
    assert result.token_usage is not None
    assert result.token_usage.total_tokens == 200


def test_immediate_token_recording_on_tool_crash() -> None:
    """P1-M1: Tokens consumed by model in step 1 are recorded when tool crashes and recovered."""
    mutating_tool = _FailingMutatingTool()

    step1_dec = ModelDecision(
        decision_id="dec_1",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(ToolCallRequest(call_id="c_1", tool_name="mutating_writer", arguments={}),),
        token_usage=TokenUsage(prompt_tokens=150, completion_tokens=50, total_tokens=200),
    )
    step2_dec = ModelDecision(
        decision_id="dec_2",
        step_id=StepId("step_1"),
        finish_reason=ModelFinishReason.STOP,
        content="Tool crash observed; terminating gracefully",
        token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec])

    runtime = AgentRuntime(model=model, tools=[mutating_tool])
    result = runtime.run("Crash task", namespace_id=NamespaceId("ns_tokens"))

    assert result.status == RunStatus.COMPLETED
    assert result.step_count == 2
    # Verify cumulative tokens include both step 1 and step 2
    assert result.total_tokens_consumed == 350
    assert result.token_usage is not None
    assert result.token_usage.total_tokens == 350
    # Step 1 tokens are preserved in step_history
    assert result.step_history[0].tokens_consumed == 200
    assert result.step_history[1].tokens_consumed == 150


def test_tool_side_effect_uncertainty_for_mutating_tools() -> None:
    """P1-M2: Failed mutating tools record OUTCOME_UNKNOWN and side-effect uncertainty."""
    # 1. Non-idempotent write failure -> OUTCOME_UNKNOWN
    non_idemp_tool = _FailingMutatingTool(side_effect=SideEffectType.NON_IDEMPOTENT_WRITE)
    model1 = FakeModelAdapter.create_tool_calling_adapter("mutating_writer", {})
    runtime1 = AgentRuntime(model=model1, tools=[non_idemp_tool])
    res1 = runtime1.run("Mutate data", namespace_id=NamespaceId("ns_side1"))

    assert len(res1.tool_results) == 1
    tr1 = res1.tool_results[0]
    assert tr1.status == ToolCallStatus.OUTCOME_UNKNOWN
    assert tr1.failure_details is not None
    assert tr1.failure_details.error_code == "SIDE_EFFECT_UNCERTAIN"

    # 2. Idempotent write failure -> OUTCOME_UNKNOWN
    idemp_tool = _FailingMutatingTool(side_effect=SideEffectType.IDEMPOTENT_WRITE)
    model2 = FakeModelAdapter.create_tool_calling_adapter("mutating_writer", {})
    runtime2 = AgentRuntime(model=model2, tools=[idemp_tool])
    res2 = runtime2.run("Idempotent mutate", namespace_id=NamespaceId("ns_side2"))

    assert len(res2.tool_results) == 1
    tr2 = res2.tool_results[0]
    assert tr2.status == ToolCallStatus.OUTCOME_UNKNOWN
    assert tr2.failure_details is not None
    assert tr2.failure_details.error_code == "SIDE_EFFECT_UNCERTAIN"


def test_run_result_provides_step_history_and_tool_results() -> None:
    """P1-M2: RunResult contains full step_history and tool_results for audit."""
    sensor = _DelayTool(clock=DeterministicClock(), delay_seconds=0.1)
    model = FakeModelAdapter.create_tool_calling_adapter(
        tool_name="delay_tool",
        arguments={},
        follow_up_content="Audit complete",
    )
    runtime = AgentRuntime(model=model, tools=[sensor])
    result = runtime.run("Audited execution", namespace_id=NamespaceId("ns_audit"))

    assert result.status == RunStatus.COMPLETED
    assert result.step_count == 2
    assert len(result.step_history) == 2
    assert result.step_history[0].step_number == 1
    assert result.step_history[1].step_number == 2
    assert len(result.tool_results) == 1
    assert result.tool_results[0].tool_name == "delay_tool"
    assert result.tool_results[0].status == ToolCallStatus.COMPLETED


def test_step_budget_boundary_single_step_completed() -> None:
    """max_steps=1, STOP on step 1 results in COMPLETED with output and history preserved."""
    dec = ModelDecision(
        decision_id="dec_single",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.STOP,
        content="Single step final answer",
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    model = FakeModelAdapter(responses=[dec])
    budget = RunBudgets(max_steps=1, max_tokens=1000, max_time_seconds=60.0)
    runtime = AgentRuntime(model=model, budgets=budget)

    result = runtime.run("Single step task", namespace_id=NamespaceId("ns_step1"))

    assert result.status == RunStatus.COMPLETED
    assert result.step_count == 1
    assert result.final_output == "Single step final answer"
    assert len(result.step_history) == 1
    assert result.step_history[0].decision.content == "Single step final answer"


def test_step_budget_boundary_multi_step_completed() -> None:
    """max_steps=2, TOOL_CALL then STOP on step 2 results in COMPLETED."""
    sensor = _DelayTool(clock=DeterministicClock(), delay_seconds=0.0)
    step1_dec = ModelDecision(
        decision_id="dec_s1",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(ToolCallRequest(call_id="c_1", tool_name="delay_tool", arguments={}),),
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    step2_dec = ModelDecision(
        decision_id="dec_s2",
        step_id=StepId("step_1"),
        finish_reason=ModelFinishReason.STOP,
        content="Two step final answer",
        token_usage=TokenUsage(prompt_tokens=15, completion_tokens=15, total_tokens=30),
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec])
    budget = RunBudgets(max_steps=2, max_tokens=1000, max_time_seconds=60.0)
    runtime = AgentRuntime(model=model, tools=[sensor], budgets=budget)

    result = runtime.run("Multi step task", namespace_id=NamespaceId("ns_step2"))

    assert result.status == RunStatus.COMPLETED
    assert result.step_count == 2
    assert result.final_output == "Two step final answer"
    assert len(result.step_history) == 2
    assert len(result.tool_results) == 1


def test_step_budget_boundary_nonterminal_final_step_exhausted() -> None:
    """Nonterminal decision on final permitted step results in BUDGET_EXHAUSTED."""
    sensor = _DelayTool(clock=DeterministicClock(), delay_seconds=0.0)
    dec = ModelDecision(
        decision_id="dec_nonterm",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(ToolCallRequest(call_id="c_1", tool_name="delay_tool", arguments={}),),
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    model = FakeModelAdapter(responses=[dec])
    budget = RunBudgets(max_steps=1, max_tokens=1000, max_time_seconds=60.0)
    runtime = AgentRuntime(model=model, tools=[sensor], budgets=budget)

    result = runtime.run("Exhaust on step 1", namespace_id=NamespaceId("ns_exhaust1"))

    assert result.status == RunStatus.BUDGET_EXHAUSTED
    assert result.step_count == 1
    assert result.failure_details is not None
    assert result.failure_details.error_code == "BUDGET_EXCEEDED"
    assert len(result.step_history) == 1
    assert len(result.tool_results) == 1


def test_step_budget_boundary_multi_step_nonterminal_final_step_exhausted() -> None:
    """max_steps=2, step 1 tool call, step 2 tool call -> BUDGET_EXHAUSTED."""
    sensor = _DelayTool(clock=DeterministicClock(), delay_seconds=0.0)
    step1_dec = ModelDecision(
        decision_id="dec_s1",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(ToolCallRequest(call_id="c_1", tool_name="delay_tool", arguments={}),),
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    step2_dec = ModelDecision(
        decision_id="dec_s2",
        step_id=StepId("step_1"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(ToolCallRequest(call_id="c_2", tool_name="delay_tool", arguments={}),),
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec])
    budget = RunBudgets(max_steps=2, max_tokens=1000, max_time_seconds=60.0)
    runtime = AgentRuntime(model=model, tools=[sensor], budgets=budget)

    result = runtime.run("Exhaust on step 2", namespace_id=NamespaceId("ns_exhaust2"))

    assert result.status == RunStatus.BUDGET_EXHAUSTED
    assert result.step_count == 2
    assert result.failure_details is not None
    assert result.failure_details.error_code == "BUDGET_EXCEEDED"
    assert len(result.step_history) == 2
    assert len(result.tool_results) == 2


def test_exact_token_budget_boundary() -> None:
    """Exact token limit tokens == max_tokens is valid; tokens == max_tokens + 1 is exhausted."""
    # Case A: tokens == max_tokens (50 == 50) -> COMPLETED
    dec_exact = ModelDecision(
        decision_id="dec_exact",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.STOP,
        content="Exact token answer",
        token_usage=TokenUsage(prompt_tokens=25, completion_tokens=25, total_tokens=50),
    )
    model_exact = FakeModelAdapter(responses=[dec_exact])
    budget = RunBudgets(max_tokens=50)
    runtime_exact = AgentRuntime(model=model_exact, budgets=budget)
    res_exact = runtime_exact.run("Exact tokens", namespace_id=NamespaceId("ns_exact_tok"))

    assert res_exact.status == RunStatus.COMPLETED
    assert res_exact.total_tokens_consumed == 50
    assert res_exact.final_output == "Exact token answer"

    # Case B: tokens == max_tokens + 1 (51 > 50) -> BUDGET_EXHAUSTED
    dec_over = ModelDecision(
        decision_id="dec_over",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.STOP,
        content="Over token answer",
        token_usage=TokenUsage(prompt_tokens=26, completion_tokens=25, total_tokens=51),
    )
    model_over = FakeModelAdapter(responses=[dec_over])
    runtime_over = AgentRuntime(model=model_over, budgets=budget)
    res_over = runtime_over.run("Over tokens", namespace_id=NamespaceId("ns_over_tok"))

    assert res_over.status == RunStatus.BUDGET_EXHAUSTED
    assert res_over.failure_details is not None
    assert res_over.failure_details.error_code == "BUDGET_EXCEEDED"


def test_exact_time_budget_boundary() -> None:
    """Exact time limit elapsed == max_time is valid; elapsed > max_time is exhausted."""
    clock = DeterministicClock()
    budget = RunBudgets(max_time_seconds=5.0)

    # Tool advances clock by exact delay
    delay_tool_exact = _DelayTool(clock=clock, delay_seconds=5.0)
    step1_dec = ModelDecision(
        decision_id="dec_1",
        step_id=StepId("step_0"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        tool_calls=(ToolCallRequest(call_id="c_1", tool_name="delay_tool", arguments={}),),
        token_usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
    )
    step2_dec = ModelDecision(
        decision_id="dec_2",
        step_id=StepId("step_1"),
        finish_reason=ModelFinishReason.STOP,
        content="Exact time answer",
        token_usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
    )
    model = FakeModelAdapter(responses=[step1_dec, step2_dec])
    runtime = AgentRuntime(model=model, tools=[delay_tool_exact], clock=clock, budgets=budget)
    res = runtime.run("Exact time", namespace_id=NamespaceId("ns_exact_time"))

    # Exactly 5.0 seconds is valid (not > 5.0s)
    assert res.status == RunStatus.COMPLETED
    assert res.final_output == "Exact time answer"

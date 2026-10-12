"""Bounded execution loop runner for the core single-agent runtime.

Orchestrates agent runs with budget enforcement, live cancellation handling,
model decision dispatching, and canonical result compilation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from memory_aware_agent_harness.contracts import (
    CancellationToken,
    ClockPort,
    ContextManifest,
    FailureDetails,
    IdGeneratorPort,
    ModelCallContext,
    ModelFinishReason,
    ModelPort,
    NamespaceId,
    RunBudgets,
    RunId,
    RunResult,
    StepId,
    StepResult,
    StepState,
    ToolProtocol,
)
from memory_aware_agent_harness.runtime.cancellation import CancellationListener
from memory_aware_agent_harness.runtime.fsm import StepFSM
from memory_aware_agent_harness.runtime.guards import BudgetGuard
from memory_aware_agent_harness.runtime.result_builder import RunResultBuilder
from memory_aware_agent_harness.runtime.state import (
    LifecycleState,
    SystemClock,
    SystemIdGenerator,
)
from memory_aware_agent_harness.runtime.step_compiler import StepOutcomeCompiler


class AgentRuntime:
    """Core single-agent runtime executing bounded tasks."""

    def __init__(
        self,
        model: ModelPort,
        *,
        tools: Sequence[ToolProtocol] | None = None,
        clock: ClockPort | None = None,
        id_generator: IdGeneratorPort | None = None,
        budgets: RunBudgets | None = None,
    ) -> None:
        self.model = model
        self.tools: tuple[ToolProtocol, ...] = tuple(tools) if tools is not None else ()
        self.clock = clock if clock is not None else SystemClock()
        self.id_generator = id_generator if id_generator is not None else SystemIdGenerator()
        self.default_budgets = budgets if budgets is not None else RunBudgets()

    def run(
        self,
        task: str,
        *,
        namespace_id: NamespaceId,
        run_id: RunId | None = None,
        budgets: RunBudgets | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> RunResult:
        """Execute a task in a bounded single-agent loop.

        Args:
            task: Task description or prompt instruction.
            namespace_id: Tenant or memory namespace identifier.
            run_id: Optional existing RunId; generated if omitted.
            budgets: Optional run budgets overriding default limits.
            cancellation_token: Optional live cooperative cancellation token.

        Returns:
            Terminal RunResult with full audit, token accounting, and final output.
        """
        if not task.strip():
            raise ValueError("Task description must not be empty")

        effective_run_id = run_id
        if effective_run_id is None:
            effective_run_id = RunId(self.id_generator.generate_id("run"))

        state = LifecycleState(
            run_id=effective_run_id,
            namespace_id=namespace_id,
            budgets=budgets or self.default_budgets,
            clock=self.clock,
            id_generator=self.id_generator,
            cancellation_token=cancellation_token,
        )

        budget_guard = BudgetGuard(state.budgets)
        cancellation_listener = CancellationListener(state)
        step_compiler = StepOutcomeCompiler(state)
        result_builder = RunResultBuilder(state)

        state.begin_run()
        final_output: str | None = None

        while not state.is_terminal:
            # Check cooperative cancellation
            if cancellation_listener.abort_if_cancelled():
                break

            # Check budget exhaustion before commencing next step
            if not budget_guard.enforce(state):
                break

            step_id, step_fsm = state.begin_step()

            try:
                step_result = self._execute_step(
                    state=state,
                    step_id=step_id,
                    step_fsm=step_fsm,
                    task=task,
                    step_compiler=step_compiler,
                    cancellation_listener=cancellation_listener,
                )
            except Exception as exc:
                failure = FailureDetails(
                    error_type=type(exc).__name__,
                    message=str(exc),
                    step_id=step_id,
                    error_code="STEP_EXECUTION_FAILURE",
                )
                step_fsm.transition_to(StepState.FAILED, reason=str(exc))
                state.fail(failure, reason=str(exc))
                break

            # Handle model decision outcome
            if step_result.decision.finish_reason == ModelFinishReason.STOP:
                final_output = step_result.decision.content
                state.complete(reason="Task completed by model")
                break
            elif step_result.decision.finish_reason == ModelFinishReason.ERROR:
                failure = step_result.decision.failure_details or FailureDetails(
                    error_type="ModelDecisionError",
                    message=step_result.decision.content or "Model returned error finish reason",
                    step_id=step_id,
                    error_code="MODEL_ERROR",
                )
                state.fail(failure, reason=failure.message)
                break

        return result_builder.build(final_output=final_output)

    def _execute_step(
        self,
        state: LifecycleState,
        step_id: StepId,
        step_fsm: StepFSM,
        task: str,
        step_compiler: StepOutcomeCompiler,
        cancellation_listener: CancellationListener,
    ) -> StepResult:
        """Execute a single step lifecycle turn."""
        # 1. Retrieval phase
        step_fsm.transition_to(StepState.RETRIEVING)
        cancellation_listener.check()

        # 2. Context compilation phase
        step_fsm.transition_to(StepState.COMPILING_CONTEXT)
        cancellation_listener.check()

        manifest = ContextManifest(
            manifest_id=self.id_generator.generate_id(f"manifest_{step_id}"),
            step_id=step_id,
            included_items=(),
            excluded_item_ids={},
            total_tokens=len(task.split()),
            budget_limit=state.budgets.max_tokens,
            compiled_prompt=task,
        )

        # 3. Model decision phase
        step_fsm.transition_to(StepState.DECIDING)
        cancellation_listener.check()

        model_ctx = ModelCallContext(
            run_id=state.run_id,
            step_id=step_id,
            namespace_id=state.namespace_id,
            budget_remaining_tokens=state.budgets.remaining_tokens(
                state.cumulative_tokens.total_tokens
            ),
            cancellation_token=state.cancellation_token,
        )

        tools_schema: list[Mapping[str, Any]] = [t.get_schema() for t in self.tools]
        decision = self.model.decide(manifest, tools_schema, model_ctx)

        # 4. Compile step outcome
        return step_compiler.compile_step(
            decision=decision,
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
        )

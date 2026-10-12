"""Bounded execution loop runner for the core single-agent runtime.

Orchestrates agent runs with budget enforcement, live cancellation handling,
model decision dispatching, 8-phase step lifecycle execution, and canonical result compilation.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from memory_aware_agent_harness.contracts import (
    CancellationToken,
    ClockPort,
    ContextItem,
    ContextManifest,
    ExecutionCancelledError,
    FailureDetails,
    IdGeneratorPort,
    MemoryKind,
    MemoryStoreProtocol,
    ModelCallContext,
    ModelFinishReason,
    ModelPort,
    NamespaceId,
    PolicyViolationError,
    RunBudgets,
    RunId,
    RunResult,
    SideEffectType,
    StepId,
    StepResult,
    StepState,
    ToolCallContext,
    ToolCallStatus,
    ToolInvocationId,
    ToolProtocol,
    ToolResult,
)
from memory_aware_agent_harness.runtime.cancellation import (
    AbortDispatcher,
    CancellationListener,
)
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
    """Core single-agent runtime executing bounded tasks across the full lifecycle."""

    def __init__(
        self,
        model: ModelPort,
        *,
        tools: Sequence[ToolProtocol] | None = None,
        memory_store: MemoryStoreProtocol | None = None,
        clock: ClockPort | None = None,
        id_generator: IdGeneratorPort | None = None,
        budgets: RunBudgets | None = None,
    ) -> None:
        self.model = model
        self.tools: tuple[ToolProtocol, ...] = tuple(tools) if tools is not None else ()
        self.tools_by_name: dict[str, ToolProtocol] = {t.name: t for t in self.tools}
        self.memory_store = memory_store
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
            except ExecutionCancelledError as exc:
                dispatcher = AbortDispatcher(state)
                dispatcher.dispatch_cancellation(reason=str(exc))
                break
            except PolicyViolationError as exc:
                dispatcher = AbortDispatcher(state)
                dispatcher.dispatch_policy_abort(exc)
                break
            except Exception as exc:
                failure = FailureDetails(
                    error_type=type(exc).__name__,
                    message=str(exc),
                    step_id=step_id,
                    error_code="STEP_EXECUTION_FAILURE",
                )
                if not step_fsm.is_terminal:
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
        """Execute the full 8-phase step lifecycle sequence."""
        # --- Phase 1: RETRIEVAL ---
        step_fsm.transition_to(StepState.RETRIEVING)
        cancellation_listener.check()

        included_items: list[ContextItem] = []
        if self.memory_store is not None:
            retrieved = self.memory_store.search(
                namespace_id=state.namespace_id,
                query=task,
                kinds=[
                    MemoryKind.WORKING,
                    MemoryKind.EPISODIC,
                    MemoryKind.SEMANTIC,
                    MemoryKind.PROCEDURAL,
                ],
                limit=5,
            )
            for m in retrieved:
                cost = len(m.content.split())
                included_items.append(
                    ContextItem(
                        memory_id=m.id,
                        kind=m.kind,
                        content=m.content,
                        rank_score=m.confidence,
                        provenance_id=str(m.id),
                        token_cost=cost,
                    )
                )

        # --- Phase 2: COMPILE CONTEXT ---
        step_fsm.transition_to(StepState.COMPILING_CONTEXT)
        cancellation_listener.check()

        manifest = ContextManifest(
            manifest_id=self.id_generator.generate_id(f"manifest_{step_id}"),
            step_id=step_id,
            included_items=tuple(included_items),
            excluded_item_ids={},
            total_tokens=sum(item.token_cost for item in included_items) + len(task.split()),
            budget_limit=state.budgets.max_tokens,
            compiled_prompt=task,
        )

        # --- Phase 3: DECIDE ---
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

        tool_results: list[ToolResult] = []

        # --- Phase 4: DISPATCH TOOL (if requested) ---
        if decision.finish_reason == ModelFinishReason.TOOL_CALL or len(decision.tool_calls) > 0:
            step_fsm.transition_to(StepState.DISPATCHING_TOOL)
            cancellation_listener.check()

            for call in decision.tool_calls:
                cancellation_listener.check()
                inv_id = ToolInvocationId(self.id_generator.generate_id(f"inv_{call.tool_name}"))
                tool = self.tools_by_name.get(call.tool_name)

                if tool is None:
                    fail_details = FailureDetails(
                        error_type="ToolNotFoundError",
                        message=f"Tool '{call.tool_name}' is not registered with runtime",
                        step_id=step_id,
                        error_code="TOOL_NOT_FOUND",
                    )
                    tool_results.append(
                        ToolResult(
                            invocation_id=inv_id,
                            tool_name=call.tool_name,
                            status=ToolCallStatus.FAILED,
                            side_effect=SideEffectType.READ_ONLY,
                            idempotency_key=f"idemp_{inv_id}",
                            error=f"Tool '{call.tool_name}' not found",
                            failure_details=fail_details,
                        )
                    )
                    continue

                tool_ctx = ToolCallContext(
                    run_id=state.run_id,
                    step_id=step_id,
                    namespace_id=state.namespace_id,
                    invocation_id=inv_id,
                    idempotency_key=f"idemp_{inv_id}",
                    cancellation_token=state.cancellation_token,
                )

                t_start = time.perf_counter()
                try:
                    raw_res = tool.execute(call.arguments, tool_ctx)
                    t_elapsed_ms = (time.perf_counter() - t_start) * 1000.0
                    tool_results.append(
                        ToolResult(
                            invocation_id=inv_id,
                            tool_name=call.tool_name,
                            status=ToolCallStatus.COMPLETED,
                            side_effect=tool.side_effect,
                            idempotency_key=f"idemp_{inv_id}",
                            result=raw_res,
                            execution_time_ms=t_elapsed_ms,
                        )
                    )
                except (ExecutionCancelledError, PolicyViolationError):
                    raise
                except Exception as exc:
                    t_elapsed_ms = (time.perf_counter() - t_start) * 1000.0
                    fail_details = FailureDetails(
                        error_type=type(exc).__name__,
                        message=str(exc),
                        step_id=step_id,
                        error_code="TOOL_EXECUTION_ERROR",
                    )
                    tool_results.append(
                        ToolResult(
                            invocation_id=inv_id,
                            tool_name=call.tool_name,
                            status=ToolCallStatus.FAILED,
                            side_effect=tool.side_effect,
                            idempotency_key=f"idemp_{inv_id}",
                            error=str(exc),
                            failure_details=fail_details,
                            execution_time_ms=t_elapsed_ms,
                        )
                    )

        # --- Phase 5: EXTRACT MEMORIES ---
        step_fsm.transition_to(StepState.EXTRACTING_MEMORIES)
        cancellation_listener.check()

        # --- Phase 6: COMMIT MEMORIES ---
        step_fsm.transition_to(StepState.COMMITTING_MEMORIES)
        cancellation_listener.check()

        # --- Phase 7: CHECKPOINTING ---
        step_fsm.transition_to(StepState.CHECKPOINTING)
        cancellation_listener.check()

        # --- Phase 8: COMPILE STEP OUTCOME & COMPLETED ---
        return step_compiler.compile_step(
            decision=decision,
            tool_results=tool_results,
            memories_proposed=0,
            memories_committed=0,
        )

"""Bounded execution loop runner for the core single-agent runtime.

Orchestrates agent runs with budget enforcement, live cancellation handling,
model decision dispatching, 8-phase step lifecycle execution, and canonical result compilation.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from memory_aware_agent_harness.contracts import (
    BudgetExceededError,
    CancellationToken,
    ClockPort,
    ContextItem,
    ContextManifest,
    DeadlineExceededError,
    DiagnosticListenerError,
    ExecutionCancelledError,
    FailureDetails,
    IdGeneratorPort,
    MemoryKind,
    MemoryStoreProtocol,
    ModelCallContext,
    ModelDecision,
    ModelFinishReason,
    ModelPort,
    NamespaceId,
    PolicyViolationError,
    RunBudgets,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepId,
    StepResult,
    StepState,
    TokenUsage,
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
from memory_aware_agent_harness.runtime.events import (
    DiagnosticEventEmitter,
    RuntimeEventListener,
    RuntimeEventType,
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


def compile_execution_prompt(
    task: str,
    history: Sequence[StepResult],
    max_history_steps: int = 10,
) -> str:
    """Compile task and bounded history of prior decisions and tool observations (P1-H1)."""
    if not history:
        return task

    def _json_default(obj: Any) -> Any:
        if isinstance(obj, Mapping):
            return dict(obj)
        return str(obj)

    bounded_history = history[-max_history_steps:]
    sections: list[str] = [f"Task: {task}", "", "--- Execution History ---"]
    for step in bounded_history:
        sections.append(f"[Step {step.step_number}]")
        if step.decision.content:
            sections.append(f"Model: {step.decision.content}")
        if step.decision.tool_calls:
            for tc in step.decision.tool_calls:
                args_json = json.dumps(tc.arguments, sort_keys=True, default=_json_default)
                sections.append(f"Action: Call {tc.tool_name} with arguments {args_json}")
        if step.tool_results:
            for tr in step.tool_results:
                obs_val = tr.result if tr.result is not None else tr.error
                if isinstance(obs_val, (dict, list, Mapping)):
                    obs_str = json.dumps(obs_val, sort_keys=True, default=_json_default)
                else:
                    obs_str = str(obs_val)
                sections.append(f"Observation ({tr.tool_name}, {tr.status.value}): {obs_str}")
        sections.append("")

    sections.append(f"--- Current Step {len(history) + 1} ---")
    sections.append("Continue executing the task based on the observations above.")
    return "\n".join(sections)


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
        event_listeners: Sequence[RuntimeEventListener] = (),
        strict_diagnostics: bool = False,
    ) -> None:
        self.model = model
        tools_list: list[ToolProtocol] = []
        seen_tool_names: set[str] = set()
        if tools is not None:
            for t in tools:
                if (
                    not getattr(t, "name", None)
                    or not isinstance(t.name, str)
                    or not t.name.strip()
                ):
                    raise ValueError(f"Tool must have a non-empty name, got {t!r}")
                if t.name in seen_tool_names:
                    raise ValueError(f"Duplicate tool registered: {t.name!r}")
                seen_tool_names.add(t.name)
                tools_list.append(t)
        self.tools: tuple[ToolProtocol, ...] = tuple(tools_list)
        self.tools_by_name: dict[str, ToolProtocol] = {t.name: t for t in self.tools}
        self.memory_store = memory_store
        self.clock = clock if clock is not None else SystemClock()
        self.id_generator = id_generator if id_generator is not None else SystemIdGenerator()
        if budgets is not None and not isinstance(budgets, RunBudgets):
            raise TypeError(
                f"budgets must be an instance of RunBudgets, got {type(budgets).__name__}"
            )
        self.default_budgets = budgets if budgets is not None else RunBudgets()
        self.event_emitter = DiagnosticEventEmitter(
            clock=self.clock,
            id_generator=self.id_generator,
            listeners=event_listeners,
            strict=strict_diagnostics,
        )

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
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Task description must not be empty or whitespace")
        if not namespace_id or not str(namespace_id).strip():
            raise ValueError("Namespace ID must not be empty or whitespace")
        if run_id is not None and not str(run_id).strip():
            raise ValueError("Run ID must not be empty or whitespace")
        if budgets is not None and not isinstance(budgets, RunBudgets):
            raise TypeError(
                f"budgets must be an instance of RunBudgets, got {type(budgets).__name__}"
            )
        if cancellation_token is not None and not isinstance(cancellation_token, CancellationToken):
            raise TypeError(
                f"cancellation_token must be an instance of CancellationToken, "
                f"got {type(cancellation_token).__name__}"
            )

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
        final_output: str | None = None

        try:
            state.begin_run()
            self.event_emitter.emit(
                RuntimeEventType.RUN_STARTED,
                run_id=state.run_id,
                payload={"task": task, "namespace_id": str(namespace_id)},
            )

            while not state.is_terminal:
                # Check cooperative cancellation
                if cancellation_listener.abort_if_cancelled():
                    break

                # Check budget exhaustion before commencing next step
                if not budget_guard.enforce(state):
                    break

                step_id, step_fsm = state.begin_step()
                self.event_emitter.emit(
                    RuntimeEventType.STEP_STARTED,
                    run_id=state.run_id,
                    step_id=step_id,
                    phase=StepState.NOT_STARTED,
                )

                try:
                    step_result = self._execute_step(
                        state=state,
                        step_id=step_id,
                        step_fsm=step_fsm,
                        task=task,
                        step_compiler=step_compiler,
                        budget_guard=budget_guard,
                        cancellation_listener=cancellation_listener,
                    )
                except BudgetExceededError as exc:
                    if not step_fsm.is_terminal:
                        step_fsm.transition_to(StepState.FAILED, reason=str(exc))
                    if not state.is_terminal:
                        state.failure_details = exc.to_failure_details(step_id=step_id)
                        state.exhaust_budget(reason=str(exc))
                    break
                except DeadlineExceededError as exc:
                    if not step_fsm.is_terminal:
                        step_fsm.transition_to(StepState.FAILED, reason=str(exc))
                    if not state.is_terminal:
                        state.failure_details = exc.to_failure_details(step_id=step_id)
                        state.exhaust_budget(reason=str(exc))
                    break
                except ExecutionCancelledError as exc:
                    dispatcher = AbortDispatcher(state)
                    dispatcher.dispatch_cancellation(reason=str(exc))
                    break
                except PolicyViolationError as exc:
                    dispatcher = AbortDispatcher(state)
                    dispatcher.dispatch_policy_abort(exc)
                    break
                except DiagnosticListenerError as exc:
                    if not step_fsm.is_terminal:
                        step_fsm.transition_to(StepState.FAILED, reason=str(exc))
                    if not state.is_terminal:
                        state.fail(exc.to_failure_details(step_id=step_id), reason=str(exc))
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

                # Record step result into execution history
                state.record_step_result(step_result)

                # Check budget exhaustion after step completion
                if not budget_guard.enforce(state):
                    break

                # Handle model decision outcome
                if step_result.decision.finish_reason == ModelFinishReason.STOP:
                    if not budget_guard.enforce(state):
                        break
                    final_output = step_result.decision.content
                    state.complete(reason="Task completed by model")
                    break
                elif step_result.decision.finish_reason == ModelFinishReason.ERROR:
                    failure = step_result.decision.failure_details or FailureDetails(
                        error_type="ModelDecisionError",
                        message=(
                            step_result.decision.content or "Model returned error finish reason"
                        ),
                        step_id=step_id,
                        error_code="MODEL_ERROR",
                    )
                    state.fail(failure, reason=failure.message)
                    break
        except DiagnosticListenerError as exc:
            if not state.is_terminal:
                state.fail(exc.to_failure_details(step_id=state.current_step_id), reason=str(exc))
        except Exception as exc:
            if not state.is_terminal:
                failure = FailureDetails(
                    error_type=type(exc).__name__,
                    message=str(exc),
                    step_id=state.current_step_id,
                    error_code="RUN_EXECUTION_FAILURE",
                )
                state.fail(failure, reason=str(exc))

        try:
            self.event_emitter.emit(
                RuntimeEventType.RUN_TERMINATED,
                run_id=state.run_id,
                payload={
                    "status": str(state.status),
                    "step_count": state.step_count,
                    "tokens_consumed": state.cumulative_tokens.total_tokens,
                },
            )
        except DiagnosticListenerError as exc:
            if not state.is_terminal:
                state.fail(exc.to_failure_details(step_id=state.current_step_id), reason=str(exc))
            elif state.failure_details is None and state.status != RunStatus.COMPLETED:
                state.failure_details = exc.to_failure_details(step_id=state.current_step_id)
        except Exception as exc:
            if not state.is_terminal:
                state.fail(
                    FailureDetails(
                        error_type=type(exc).__name__,
                        message=str(exc),
                        step_id=state.current_step_id,
                        error_code="RUN_TERMINATED_FAILURE",
                    ),
                    reason=str(exc),
                )
        return result_builder.build(final_output=final_output)

    def _execute_step(
        self,
        state: LifecycleState,
        step_id: StepId,
        step_fsm: StepFSM,
        task: str,
        step_compiler: StepOutcomeCompiler,
        budget_guard: BudgetGuard,
        cancellation_listener: CancellationListener,
    ) -> StepResult:
        """Execute the full 8-phase step lifecycle sequence via modular phase handlers."""
        # Phase 1: Context retrieval
        included_items = self._phase_1_retrieve(
            state, step_id, step_fsm, task, cancellation_listener
        )

        # Phase 2: Compile context manifest with bounded history
        manifest = self._phase_2_compile_context(
            state, step_id, step_fsm, task, included_items, cancellation_listener
        )

        # Phase 3: Model decision dispatch
        decision = self._phase_3_decide(
            state, step_id, step_fsm, manifest, budget_guard, cancellation_listener
        )

        # Phase 4: Tool dispatch
        tool_results = self._phase_4_dispatch_tools(
            state, step_id, step_fsm, decision, budget_guard, cancellation_listener
        )

        # Phases 5-7: Memory extraction, commit, and checkpoint lifecycle stubs
        self._phase_5_to_7_lifecycle_stubs(state, step_id, step_fsm, cancellation_listener)

        # Phase 8: Outcome compilation
        return self._phase_8_compile_outcome(state, step_id, decision, tool_results, step_compiler)

    def _phase_1_retrieve(
        self,
        state: LifecycleState,
        step_id: StepId,
        step_fsm: StepFSM,
        task: str,
        cancellation_listener: CancellationListener,
    ) -> list[ContextItem]:
        step_fsm.transition_to(StepState.RETRIEVING)
        self.event_emitter.emit(
            RuntimeEventType.PHASE_TRANSITION,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.RETRIEVING,
        )
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
        return included_items

    def _phase_2_compile_context(
        self,
        state: LifecycleState,
        step_id: StepId,
        step_fsm: StepFSM,
        task: str,
        included_items: list[ContextItem],
        cancellation_listener: CancellationListener,
    ) -> ContextManifest:
        step_fsm.transition_to(StepState.COMPILING_CONTEXT)
        self.event_emitter.emit(
            RuntimeEventType.PHASE_TRANSITION,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.COMPILING_CONTEXT,
        )
        cancellation_listener.check()

        compiled_prompt = compile_execution_prompt(task, state.step_history, max_history_steps=10)
        prompt_token_est = len(compiled_prompt.split())

        return ContextManifest(
            manifest_id=self.id_generator.generate_id(f"manifest_{step_id}"),
            step_id=step_id,
            included_items=tuple(included_items),
            excluded_item_ids={},
            total_tokens=sum(item.token_cost for item in included_items) + prompt_token_est,
            budget_limit=state.budgets.max_tokens,
            compiled_prompt=compiled_prompt,
        )

    def _phase_3_decide(
        self,
        state: LifecycleState,
        step_id: StepId,
        step_fsm: StepFSM,
        manifest: ContextManifest,
        budget_guard: BudgetGuard,
        cancellation_listener: CancellationListener,
    ) -> ModelDecision:
        step_fsm.transition_to(StepState.DECIDING)
        self.event_emitter.emit(
            RuntimeEventType.PHASE_TRANSITION,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.DECIDING,
        )
        cancellation_listener.check()

        deadline = state.started_at + timedelta(seconds=state.budgets.max_time_seconds)
        now = state.clock.now()
        if now >= deadline:
            state.exhaust_budget(reason="Time budget exceeded before model invocation")
            raise DeadlineExceededError(
                f"Deadline exceeded for run '{state.run_id}' step '{step_id}'",
                deadline=deadline,
            )

        model_ctx = ModelCallContext(
            run_id=state.run_id,
            step_id=step_id,
            namespace_id=state.namespace_id,
            budget_remaining_tokens=state.budgets.remaining_tokens(
                state.cumulative_tokens.total_tokens
            ),
            deadline=deadline,
            cancellation_token=state.cancellation_token,
        )

        tools_schema: list[Mapping[str, Any]] = [t.get_schema() for t in self.tools]
        self.event_emitter.emit(
            RuntimeEventType.MODEL_INVOKED,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.DECIDING,
        )
        decision = self.model.decide(manifest, tools_schema, model_ctx)
        self.event_emitter.emit(
            RuntimeEventType.MODEL_RESPONDED,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.DECIDING,
            payload={
                "finish_reason": str(decision.finish_reason),
                "tool_calls_count": len(decision.tool_calls),
            },
        )

        # Record token usage immediately upon model return (P1-M1)
        eff_tokens = decision.token_usage
        if eff_tokens is None:
            if decision.tokens_used > 0:
                eff_tokens = TokenUsage(
                    prompt_tokens=0,
                    completion_tokens=decision.tokens_used,
                    total_tokens=decision.tokens_used,
                )
            else:
                eff_tokens = TokenUsage.zero()
        state.record_tokens(eff_tokens)

        # Enforce budget immediately after model return (P1-H2)
        if not budget_guard.enforce(state):
            breach = budget_guard.evaluate_budget(state)
            raise breach or BudgetExceededError(
                f"Budget exceeded after model step: {state.cumulative_tokens.total_tokens} tokens",
                budget_type="tokens",
                limit=state.budgets.max_tokens,
                observed=state.cumulative_tokens.total_tokens,
            )

        return decision

    def _phase_4_dispatch_tools(
        self,
        state: LifecycleState,
        step_id: StepId,
        step_fsm: StepFSM,
        decision: ModelDecision,
        budget_guard: BudgetGuard,
        cancellation_listener: CancellationListener,
    ) -> list[ToolResult]:
        if decision.finish_reason != ModelFinishReason.TOOL_CALL and len(decision.tool_calls) == 0:
            return []

        step_fsm.transition_to(StepState.DISPATCHING_TOOL)
        self.event_emitter.emit(
            RuntimeEventType.PHASE_TRANSITION,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.DISPATCHING_TOOL,
        )
        cancellation_listener.check()

        tool_results: list[ToolResult] = []
        deadline = state.started_at + timedelta(seconds=state.budgets.max_time_seconds)

        for call in decision.tool_calls:
            cancellation_listener.check()
            if not budget_guard.enforce(state):
                break

            self.event_emitter.emit(
                RuntimeEventType.TOOL_INVOKED,
                run_id=state.run_id,
                step_id=step_id,
                phase=StepState.DISPATCHING_TOOL,
                payload={"tool_name": call.tool_name, "call_id": call.call_id},
            )
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
                self.event_emitter.emit(
                    RuntimeEventType.TOOL_COMPLETED,
                    run_id=state.run_id,
                    step_id=step_id,
                    phase=StepState.DISPATCHING_TOOL,
                    payload={"tool_name": call.tool_name, "status": str(ToolCallStatus.FAILED)},
                )
                continue

            tool_ctx = ToolCallContext(
                run_id=state.run_id,
                step_id=step_id,
                namespace_id=state.namespace_id,
                invocation_id=inv_id,
                idempotency_key=f"idemp_{inv_id}",
                deadline=deadline,
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
                is_mutating = tool.side_effect in (
                    SideEffectType.IDEMPOTENT_WRITE,
                    SideEffectType.NON_IDEMPOTENT_WRITE,
                )
                status = ToolCallStatus.OUTCOME_UNKNOWN if is_mutating else ToolCallStatus.FAILED
                error_code = "SIDE_EFFECT_UNCERTAIN" if is_mutating else "TOOL_EXECUTION_ERROR"
                fail_details = FailureDetails(
                    error_type=type(exc).__name__,
                    message=str(exc),
                    step_id=step_id,
                    error_code=error_code,
                )
                tool_results.append(
                    ToolResult(
                        invocation_id=inv_id,
                        tool_name=call.tool_name,
                        status=status,
                        side_effect=tool.side_effect,
                        idempotency_key=f"idemp_{inv_id}",
                        error=str(exc),
                        failure_details=fail_details,
                        execution_time_ms=t_elapsed_ms,
                    )
                )

            self.event_emitter.emit(
                RuntimeEventType.TOOL_COMPLETED,
                run_id=state.run_id,
                step_id=step_id,
                phase=StepState.DISPATCHING_TOOL,
                payload={
                    "tool_name": call.tool_name,
                    "status": str(tool_results[-1].status),
                },
            )

            # Enforce budget after each tool invocation
            if not budget_guard.enforce(state):
                break

        return tool_results

    def _phase_5_to_7_lifecycle_stubs(
        self,
        state: LifecycleState,
        step_id: StepId,
        step_fsm: StepFSM,
        cancellation_listener: CancellationListener,
    ) -> None:
        """Execute extension point stubs for memory extraction, commit, and checkpointing."""
        # Phase 5: EXTRACT MEMORIES
        step_fsm.transition_to(StepState.EXTRACTING_MEMORIES)
        self.event_emitter.emit(
            RuntimeEventType.PHASE_TRANSITION,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.EXTRACTING_MEMORIES,
        )
        cancellation_listener.check()

        # Phase 6: COMMIT MEMORIES
        step_fsm.transition_to(StepState.COMMITTING_MEMORIES)
        self.event_emitter.emit(
            RuntimeEventType.PHASE_TRANSITION,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.COMMITTING_MEMORIES,
        )
        cancellation_listener.check()

        # Phase 7: CHECKPOINTING
        step_fsm.transition_to(StepState.CHECKPOINTING)
        self.event_emitter.emit(
            RuntimeEventType.PHASE_TRANSITION,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.CHECKPOINTING,
        )
        cancellation_listener.check()

    def _phase_8_compile_outcome(
        self,
        state: LifecycleState,
        step_id: StepId,
        decision: ModelDecision,
        tool_results: list[ToolResult],
        step_compiler: StepOutcomeCompiler,
    ) -> StepResult:
        step_result = step_compiler.compile_step(
            decision=decision,
            tool_results=tool_results,
            memories_proposed=0,
            memories_committed=0,
        )
        self.event_emitter.emit(
            RuntimeEventType.STEP_COMPLETED,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.COMPLETED,
            payload={
                "step_id": str(step_id),
                "finish_reason": str(decision.finish_reason),
            },
        )
        return step_result

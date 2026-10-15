"""Bounded execution loop runner for the core single-agent runtime.

Orchestrates agent runs with budget enforcement, live cancellation handling,
model decision dispatching, 8-phase step lifecycle execution, and canonical result compilation.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import timedelta
from typing import Any, Literal, cast

from memory_aware_agent_harness.contracts import (
    BudgetExceededError,
    CancellationToken,
    CheckpointId,
    ClockPort,
    ContextItem,
    ContextManifest,
    DeadlineExceededError,
    DiagnosticListenerError,
    DirtyRollbackError,
    ExecutionCancelledError,
    FailureDetails,
    IdGeneratorPort,
    MemoryFilter,
    MemoryId,
    MemoryKind,
    MemoryState,
    MemoryStoreProtocol,
    ModelCallContext,
    ModelDecision,
    ModelFinishReason,
    ModelPort,
    NamespaceId,
    PolicyViolationError,
    RecordNotFoundError,
    RunBudgets,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepId,
    StepResult,
    StepState,
    StorageError,
    StoragePort,
    TokenUsage,
    ToolCallContext,
    ToolCallStatus,
    ToolInvocationId,
    ToolProtocol,
    ToolReconciliationReport,
    ToolResult,
    compute_state_hash,
)
from memory_aware_agent_harness.runtime.cancellation import (
    AbortDispatcher,
    CancellationListener,
)
from memory_aware_agent_harness.runtime.events import (
    DiagnosticEventEmitter,
    RuntimeEvent,
    RuntimeEventListener,
    RuntimeEventType,
)
from memory_aware_agent_harness.runtime.fsm import StepFSM
from memory_aware_agent_harness.runtime.guards import BudgetGuard
from memory_aware_agent_harness.runtime.recovery import OrphanedRunReport
from memory_aware_agent_harness.runtime.rehydration import (
    CheckpointRehydrationEngine,
    RehydratedState,
)
from memory_aware_agent_harness.runtime.result_builder import RunResultBuilder
from memory_aware_agent_harness.runtime.state import (
    LifecycleState,
    SystemClock,
    SystemIdGenerator,
)
from memory_aware_agent_harness.runtime.step_compiler import StepOutcomeCompiler
from memory_aware_agent_harness.storage.checkpoints_repo import (
    DEFAULT_BRANCH_ID,
    CheckpointRecord,
)
from memory_aware_agent_harness.storage.tools_repo import compute_argument_fingerprint

logger = logging.getLogger(__name__)


class _StorageTraceCollector(RuntimeEventListener):
    """Event listener that synchronously records runtime events as trace events in storage."""

    def __init__(self, st: StoragePort, ns: NamespaceId) -> None:
        self._st = st
        self._ns = ns
        self.last_error: Exception | None = None

    def on_event(self, event: RuntimeEvent) -> None:
        try:
            self._st.append_trace_event(
                event_id=event.event_id,
                run_id=event.run_id,
                namespace_id=self._ns,
                sequence_num=event.sequence_number,
                event_type=(
                    event.event_type.value
                    if hasattr(event.event_type, "value")
                    else str(event.event_type)
                ),
                payload=event.payload,
                emitted_at=event.timestamp,
                step_id=event.step_id,
            )
        except Exception as exc:
            self.last_error = exc
            raise


def compile_execution_prompt(
    task: str,
    history: Sequence[StepResult],
    max_history_steps: int = 10,
    max_observation_chars: int = 4000,
    max_history_chars: int = 16000,
    *,
    context_items: Sequence[ContextItem] = (),
) -> str:
    """Compile task, relevant retrieved context, and bounded history (P1-H1, P2-T21).

    Enforces strict character-level bounds on individual observations (max_observation_chars)
    and overall accumulated history text (max_history_chars) to prevent prompt explosion
    from huge or unbroken tool outputs. Truncated observations append '... [truncated]'.

    Note:
        Full prompt fencing, boundary delimiters, and indirect prompt injection defenses
        belong to Phase 3 (Context Compilation Engine).
    """
    if not history and not context_items:
        return task

    def _json_default(obj: Any) -> Any:
        if isinstance(obj, Mapping):
            return dict(obj)
        return str(obj)

    sections: list[str] = [f"Task: {task}"]

    if context_items:
        sections.append("")
        sections.append("--- Relevant Context ---")
        for item in context_items:
            kind_str = (
                item.kind.value.upper() if hasattr(item.kind, "value") else str(item.kind).upper()
            )
            sections.append(f"[{kind_str}] (score: {item.rank_score:.2f}) {item.content}")

    if not history:
        return "\n".join(sections)

    sections.append("")
    sections.append("--- Execution History ---")
    history_entries: list[str] = []

    for step in history[-max_history_steps:]:
        step_lines: list[str] = [f"[Step {step.step_number}]"]
        if step.decision.content:
            step_lines.append(f"Model: {step.decision.content}")
        if step.decision.tool_calls:
            for tc in step.decision.tool_calls:
                args_json = json.dumps(tc.arguments, sort_keys=True, default=_json_default)
                step_lines.append(f"Action: Call {tc.tool_name} with arguments {args_json}")
        if step.tool_results:
            for tr in step.tool_results:
                obs_val = tr.result if tr.result is not None else tr.error
                if isinstance(obs_val, dict | list | Mapping):
                    obs_str = json.dumps(obs_val, sort_keys=True, default=_json_default)
                else:
                    obs_str = str(obs_val)

                if len(obs_str) > max_observation_chars:
                    obs_str = obs_str[:max_observation_chars] + "... [truncated]"

                step_lines.append(f"Observation ({tr.tool_name}, {tr.status.value}): {obs_str}")
        step_lines.append("")
        history_entries.append("\n".join(step_lines))

    # Enforce total history text character limit (max_history_chars)
    combined_history = "\n".join(history_entries)
    if len(combined_history) > max_history_chars:
        accumulated: list[str] = []
        current_len = 0
        for entry in reversed(history_entries):
            if current_len + len(entry) + 1 <= max_history_chars:
                accumulated.append(entry)
                current_len += len(entry) + 1
            else:
                if not accumulated:
                    accumulated.append(entry[:max_history_chars] + "... [truncated]")
                break
        history_entries = list(reversed(accumulated))

    sections.extend(history_entries)
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
        storage: StoragePort | None = None,
        clock: ClockPort | None = None,
        id_generator: IdGeneratorPort | None = None,
        budgets: RunBudgets | None = None,
        event_listeners: Sequence[RuntimeEventListener] = (),
        strict_diagnostics: bool = False,
        business_operation_id: str | None = None,
        business_operation_ids: Mapping[str, str] | None = None,
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
        self.storage = storage
        self.memory_store: MemoryStoreProtocol | None
        if memory_store is not None:
            self.memory_store = memory_store
        elif storage is not None and isinstance(storage, MemoryStoreProtocol):
            self.memory_store = storage
        else:
            self.memory_store = None
        self.clock = clock if clock is not None else SystemClock()
        self.id_generator = id_generator if id_generator is not None else SystemIdGenerator()
        if budgets is not None and not isinstance(budgets, RunBudgets):
            raise TypeError(
                f"budgets must be an instance of RunBudgets, got {type(budgets).__name__}"
            )
        self.default_budgets = budgets if budgets is not None else RunBudgets()
        self.default_business_operation_id = business_operation_id
        self.default_business_operation_ids = (
            dict(business_operation_ids) if business_operation_ids else {}
        )
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
        business_operation_id: str | None = None,
        business_operation_ids: Mapping[str, str] | None = None,
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
        if not isinstance(namespace_id, str):
            raise TypeError(f"Namespace ID must be str, got {type(namespace_id).__name__}")
        if not namespace_id.strip():
            raise ValueError("Namespace ID must not be empty or whitespace")
        if run_id is not None:
            if not isinstance(run_id, str):
                raise TypeError(f"Run ID must be str, got {type(run_id).__name__}")
            if not run_id.strip():
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

        eff_biz_id = business_operation_id or self.default_business_operation_id
        eff_biz_ids = dict(self.default_business_operation_ids)
        if business_operation_ids:
            eff_biz_ids.update(business_operation_ids)

        state = LifecycleState(
            run_id=effective_run_id,
            namespace_id=namespace_id,
            budgets=budgets or self.default_budgets,
            clock=self.clock,
            id_generator=self.id_generator,
            cancellation_token=cancellation_token,
            business_operation_id=eff_biz_id,
            business_operation_ids=eff_biz_ids,
        )

        budget_guard = BudgetGuard(state.budgets)
        cancellation_listener = CancellationListener(state)
        step_compiler = StepOutcomeCompiler(state)
        result_builder = RunResultBuilder(state)
        final_output: str | None = None

        trace_collector: _StorageTraceCollector | None = None
        if self.storage is not None:
            trace_collector = _StorageTraceCollector(self.storage, state.namespace_id)
            self.event_emitter.add_listener(trace_collector)

        try:
            state.begin_run()
            if self.storage is not None:
                initial_run = RunResult(
                    run_id=state.run_id,
                    namespace_id=state.namespace_id,
                    status=RunStatus.RUNNING,
                    step_count=0,
                    total_tokens_consumed=0,
                    final_output=None,
                    trace_id=f"trace_{state.run_id}",
                    error=None,
                    token_usage=TokenUsage.zero(),
                    step_history=(),
                    tool_results=(),
                )
                self.storage.save_run(initial_run)

            self.event_emitter.emit(
                RuntimeEventType.RUN_STARTED,
                run_id=state.run_id,
                payload={"task": task, "namespace_id": str(namespace_id)},
            )

            while not state.is_terminal:
                # Check cooperative cancellation
                if cancellation_listener.abort_if_cancelled():
                    break

                # Check budget exhaustion before commencing next step (pre-step check)
                if not budget_guard.enforce_pre_step(state):
                    break

                step_id, step_fsm = state.begin_step()
                if self.storage is not None:
                    self.storage.record_step_start(
                        step_id=step_id,
                        run_id=state.run_id,
                        namespace_id=state.namespace_id,
                        step_number=state.step_count,
                        started_at=self.clock.now(),
                    )
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

                # Check budget exhaustion after step completion (post-step check)
                is_terminal_decision = step_result.decision.finish_reason == ModelFinishReason.STOP
                if not budget_guard.enforce_post_step(state, is_terminal=is_terminal_decision):
                    break

                # Handle model decision outcome
                if is_terminal_decision:
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

        # Dispatch terminal lifecycle event (RUN_TERMINATED).
        # Contract (P1-M5, P2-04):
        # - In strict mode, if a listener fails during RUN_TERMINATED:
        #   * If the run is not yet terminal, it transitions to FAILED
        #     with DIAGNOSTIC_LISTENER_FAILURE.
        #   * If the run is already COMPLETED (or terminal), the valid terminal
        #     outcome is preserved without being retroactively corrupted;
        #     the listener error is logged at ERROR level.
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
            logger.error(
                "Diagnostic listener error during RUN_TERMINATED dispatch: %s",
                exc,
            )
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
            logger.error(
                "Unexpected error during RUN_TERMINATED dispatch: %s",
                exc,
            )
        finally:
            if trace_collector is not None:
                self.event_emitter.remove_listener(trace_collector)

        final_run_result = result_builder.build(final_output=final_output)

        # Mandatory trace persistence verification:
        # A run must not report successful execution if trace event persistence failed.
        if (
            trace_collector is not None
            and trace_collector.last_error is not None
            and final_run_result.status == RunStatus.COMPLETED
        ):
            trace_err = trace_collector.last_error
            fail_details = FailureDetails(
                error_type=type(trace_err).__name__,
                error_code="TRACE_PERSISTENCE_FAILURE",
                message=f"Mandatory trace persistence failed: {trace_err}",
            )
            final_run_result = dataclasses.replace(
                final_run_result,
                status=RunStatus.FAILED,
                failure_details=fail_details,
                final_output=None,
                error=fail_details.message,
            )

        if self.storage is not None:
            try:
                self.storage.save_run(final_run_result)
            except Exception as exc:
                logger.error("Failed to save terminal run state to storage: %s", exc)
                if self.event_emitter._strict:
                    raise
                if final_run_result.status == RunStatus.COMPLETED:
                    fail_details = FailureDetails(
                        error_type=type(exc).__name__,
                        error_code="STORAGE_PERSISTENCE_FAILURE",
                        message=f"Mandatory run state persistence failed: {exc}",
                    )
                    final_run_result = dataclasses.replace(
                        final_run_result,
                        status=RunStatus.FAILED,
                        failure_details=fail_details,
                        final_output=None,
                        error=fail_details.message,
                    )
        return final_run_result

    def restore_from_checkpoint(
        self,
        checkpoint_id: CheckpointId,
        namespace_id: NamespaceId,
        *,
        critical_memory_ids: Sequence[MemoryId] = (),
        conflict_strategy: Literal["refuse", "purge"] = "purge",
    ) -> tuple[LifecycleState, RehydratedState]:
        """Restore LifecycleState from checkpoint with anti-resurrection tombstone checks."""
        if self.storage is None:
            raise StorageError("Cannot restore from checkpoint without storage configured")

        if hasattr(self.storage, "get_checkpoint_record"):
            checkpoint_rec = self.storage.get_checkpoint_record(checkpoint_id, namespace_id)
        else:
            raw_payload = self.storage.get_checkpoint(checkpoint_id, namespace_id)
            if raw_payload is None:
                raise RecordNotFoundError(
                    f"Checkpoint '{checkpoint_id}' not found",
                    record_id=str(checkpoint_id),
                    namespace_id=str(namespace_id),
                    table="checkpoints",
                )
            checkpoint_rec = CheckpointRecord(
                checkpoint_id=checkpoint_id,
                run_id=RunId(str(raw_payload.get("run_id", "run_restored"))),
                step_id=StepId(str(raw_payload.get("step_id", "step_restored"))),
                branch_id=DEFAULT_BRANCH_ID,
                namespace_id=namespace_id,
                parent_checkpoint_id=None,
                state_payload_json=json.dumps(dict(raw_payload), sort_keys=True),
                state_hash=compute_state_hash(raw_payload),
                created_at=self.clock.now(),
            )

        if checkpoint_rec is None:
            raise RecordNotFoundError(
                f"Checkpoint '{checkpoint_id}' not found in namespace '{namespace_id}'",
                record_id=str(checkpoint_id),
                namespace_id=str(namespace_id),
                table="checkpoints",
            )

        engine = CheckpointRehydrationEngine(self.storage)
        return engine.rehydrate_lifecycle_state(
            checkpoint=checkpoint_rec,
            namespace_id=namespace_id,
            clock=self.clock,
            id_generator=self.id_generator,
            budgets=self.default_budgets,
            critical_memory_ids=critical_memory_ids,
            conflict_strategy=conflict_strategy,
        )

    def reconcile_orphaned_runs(
        self,
        namespace_id: NamespaceId,
        *,
        strategy: Literal["fail", "pause"] = "fail",
        reason: str | None = None,
    ) -> tuple[OrphanedRunReport, ...]:
        """Scan and reconcile any runs left in non-terminal states in persistent storage.

        Audits in-flight steps and mutating tool calls:
        - If mutating tools were in flight, marks PARTIAL_DIRTY_ROLLBACK.
        - Reconciles run to FAILED or PAUSED.
        - Marks pending tool invocations as OUTCOME_UNKNOWN.
        - Fails any running steps.

        Args:
            namespace_id: Tenant namespace to inspect.
            strategy: 'fail' or 'pause'.
            reason: Optional custom failure explanation.

        Returns:
            Tuple of OrphanedRunReport records describing each reconciled run.
        """
        if self.storage is None:
            return ()

        if hasattr(self.storage, "reconcile_all_orphaned_runs"):
            return cast(
                tuple[OrphanedRunReport, ...],
                self.storage.reconcile_all_orphaned_runs(
                    namespace_id=namespace_id,
                    strategy=strategy,
                    reason=reason,
                ),
            )

        # Fallback for generic StoragePort
        orphaned = self.storage.list_orphaned_runs(namespace_id)
        reports: list[OrphanedRunReport] = []
        for run in orphaned:
            report = OrphanedRunReport(
                run_id=run.run_id,
                namespace_id=run.namespace_id,
                previous_status=run.status,
                new_status=RunStatus.FAILED if strategy == "fail" else RunStatus.PAUSED,
                is_dirty=False,
                mutating_tool_invocations_count=0,
                reconciled_steps_count=0,
                reason=reason or f"Orphaned run reconciled to {strategy}",
                failure_details=FailureDetails(
                    error_type="OrphanedRunError",
                    message=reason or f"Orphaned run reconciled to {strategy}",
                    error_code="ORPHANED_RUN_DETECTED",
                ),
            )
            reports.append(report)
        return tuple(reports)

    def reconcile_tool_invocations(
        self,
        namespace_id: NamespaceId,
        run_id: RunId | None = None,
    ) -> ToolReconciliationReport:
        """Scan and reconcile in-flight tool invocations left pending across crashes."""
        if self.storage is None:
            return ToolReconciliationReport(
                namespace_id=namespace_id,
                reconciled_count=0,
                cached_replayed_count=0,
                marked_unknown_count=0,
                dirty_non_idempotent_count=0,
                run_id=run_id,
            )
        if hasattr(self.storage, "reconcile_tool_invocations"):
            return cast(
                ToolReconciliationReport,
                self.storage.reconcile_tool_invocations(namespace_id, run_id=run_id),
            )
        from memory_aware_agent_harness.runtime.tool_recovery import ToolRecoveryEngine

        engine = ToolRecoveryEngine(self.storage)
        return engine.reconcile_pending_invocations(namespace_id, run_id=run_id)

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
        decision: ModelDecision | None = None
        tool_results: list[ToolResult] = []
        try:
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
            self._phase_4_dispatch_tools(
                state,
                step_id,
                step_fsm,
                decision,
                budget_guard,
                cancellation_listener,
                tool_results=tool_results,
            )

            # Phases 5-7: Memory extraction, commit, and checkpoint lifecycle stubs
            checkpoint = self._phase_5_to_7_lifecycle_stubs(
                state, step_id, step_fsm, cancellation_listener
            )

            # Phase 8: Outcome compilation
            return self._phase_8_compile_outcome(
                state,
                step_id,
                decision,
                tool_results,
                step_compiler,
                checkpoint=checkpoint,
            )
        except BaseException as exc:
            # Preserve partial step and tool execution history on aborted/failed steps
            effective_decision = decision if decision is not None else state.current_step_decision
            has_step = any(s.step_id == step_id for s in state.step_history)
            if effective_decision is not None and not has_step:
                to_fd = getattr(exc, "to_failure_details", None)
                fail_details = to_fd(step_id=step_id) if callable(to_fd) else None
                if fail_details is None:
                    fail_details = FailureDetails(
                        error_type=type(exc).__name__,
                        message=str(exc),
                        step_id=step_id,
                        error_code="STEP_ABORTED",
                    )
                target_state = (
                    StepState.ABORTED
                    if isinstance(exc, ExecutionCancelledError | PolicyViolationError)
                    else StepState.FAILED
                )
                partial_step = step_compiler.compile_step(
                    decision=effective_decision,
                    tool_results=tool_results,
                    failure_details=fail_details,
                    target_state=target_state,
                )
                state.record_step_result(partial_step)
                if self.storage is not None:
                    with suppress(Exception):
                        self.storage.save_step(partial_step, state.namespace_id)
            raise

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
        target_store = self.storage if self.storage is not None else self.memory_store
        if target_store is not None and hasattr(target_store, "search_scored"):
            query_filter = MemoryFilter(
                namespace_id=state.namespace_id,
                state=MemoryState.ACTIVE,
                as_of=self.clock.now(),
                query_text=task,
                limit=5,
            )
            scored_results = target_store.search_scored(query_filter)
            for item in scored_results:
                record = item.record
                cost = max(1, len(record.content.split()))
                prov_id = (
                    str(record.origin.value)
                    if hasattr(record.origin, "value")
                    else str(record.origin)
                )
                included_items.append(
                    ContextItem(
                        memory_id=record.id,
                        kind=record.kind,
                        content=record.content,
                        rank_score=item.score,
                        provenance_id=prov_id,
                        token_cost=cost,
                    )
                )
        elif self.memory_store is not None:
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
                cost = max(1, len(m.content.split()))
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

        base_prompt = compile_execution_prompt(task, state.step_history, max_history_steps=10)
        base_prompt_tokens = len(base_prompt.split())

        sorted_candidates = sorted(included_items, key=lambda item: item.rank_score, reverse=True)

        remaining_budget = state.budgets.remaining_tokens(state.cumulative_tokens.total_tokens)
        available_context_tokens = max(0, remaining_budget - base_prompt_tokens)

        allocated_items: list[ContextItem] = []
        excluded_item_ids: dict[MemoryId, str] = {}
        current_context_tokens = 0

        for item in sorted_candidates:
            if current_context_tokens + item.token_cost <= available_context_tokens:
                allocated_items.append(item)
                current_context_tokens += item.token_cost
            else:
                excluded_item_ids[item.memory_id] = "BUDGET_EXCEEDED"

        compiled_prompt = compile_execution_prompt(
            task,
            state.step_history,
            max_history_steps=10,
            context_items=allocated_items,
        )
        total_tokens = current_context_tokens + base_prompt_tokens

        return ContextManifest(
            manifest_id=self.id_generator.generate_id(f"manifest_{step_id}"),
            step_id=step_id,
            included_items=tuple(allocated_items),
            excluded_item_ids=excluded_item_ids,
            total_tokens=total_tokens,
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
        if now > deadline:
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
        state.current_step_decision = decision

        # Enforce budget immediately after model return (P1-H2)
        if not budget_guard.enforce_mid_step(state):
            breach = budget_guard.evaluate_mid_step_budget(state)
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
        tool_results: list[ToolResult] | None = None,
    ) -> list[ToolResult]:
        if decision.finish_reason != ModelFinishReason.TOOL_CALL and len(decision.tool_calls) == 0:
            return tool_results if tool_results is not None else []

        step_fsm.transition_to(StepState.DISPATCHING_TOOL)
        self.event_emitter.emit(
            RuntimeEventType.PHASE_TRANSITION,
            run_id=state.run_id,
            step_id=step_id,
            phase=StepState.DISPATCHING_TOOL,
        )
        cancellation_listener.check()

        if tool_results is None:
            tool_results = []
        deadline = state.started_at + timedelta(seconds=state.budgets.max_time_seconds)

        for call in decision.tool_calls:
            cancellation_listener.check()
            if not budget_guard.enforce_mid_step(state):
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
            side_effect = tool.side_effect if tool is not None else SideEffectType.READ_ONLY
            is_non_idemp = side_effect == SideEffectType.NON_IDEMPOTENT_WRITE
            supports_provider_idemp = bool(getattr(tool, "supports_provider_idempotency", False))

            # Store and check canonical argument fingerprint
            fingerprint = compute_argument_fingerprint(call.arguments)

            # Prior uncertain invocation lookup for non-idempotent operations
            prior_uncertain = None
            if (
                self.storage is not None
                and is_non_idemp
                and hasattr(self.storage, "find_uncertain_invocation_by_fingerprint")
            ):
                prior_uncertain = self.storage.find_uncertain_invocation_by_fingerprint(
                    namespace_id=state.namespace_id,
                    tool_name=call.tool_name,
                    argument_fingerprint=fingerprint,
                )

            # Separate invocation identity from business-operation identity.
            # Model-supplied call IDs must NEVER serve as the sole trusted identity
            # for external operations.
            # Model-provided arguments must not override trusted idempotency policy.
            # Model-supplied 'idempotency_key' is strictly ignored.
            trusted_biz_id = state.get_business_operation_id(call.tool_name, call.call_id)
            if trusted_biz_id:
                # Caller-supplied trusted business operation ID overrides any model arguments
                idemp_key = trusted_biz_id
            elif (
                tool is not None
                and hasattr(tool, "get_business_operation_id")
                and callable(getattr(tool, "get_business_operation_id", None))
            ):
                biz_func = getattr(tool, "get_business_operation_id")  # noqa: B009
                idemp_key = str(biz_func(call.arguments, state))
            elif supports_provider_idemp and prior_uncertain is not None:
                # Genuine retry of an uncertain external operation: adopt the prior idempotency key
                # so the external provider can deduplicate the request across crashes/retries!
                idemp_key = prior_uncertain.idempotency_key
            elif supports_provider_idemp:
                idemp_key = f"prov_{call.tool_name}_{inv_id}"
            else:
                idemp_key = str(inv_id)

            dispatched_at = self.clock.now()

            if is_non_idemp and not supports_provider_idemp and prior_uncertain is not None:
                # Without verified provider idempotency, repeating an uncertain non-idempotent
                # operation is unsafe and must be blocked with PARTIAL_DIRTY_ROLLBACK!
                fail_details = FailureDetails(
                    error_type="DirtyRollbackError",
                    message=(
                        f"Non-idempotent tool '{call.tool_name}' has prior uncertain "
                        f"invocation '{prior_uncertain.invocation_id}' without provider "
                        "idempotency. Automatic retry blocked to prevent duplicate side effects."
                    ),
                    step_id=step_id,
                    error_code="PARTIAL_DIRTY_ROLLBACK",
                    recovery_hint="Manual operator intervention or reconciliation required",
                )
                blocked_res = ToolResult(
                    invocation_id=inv_id,
                    tool_name=call.tool_name,
                    status=ToolCallStatus.FAILED,
                    side_effect=side_effect,
                    idempotency_key=idemp_key,
                    error=fail_details.message,
                    failure_details=fail_details,
                )
                tool_results.append(blocked_res)
                self.event_emitter.emit(
                    RuntimeEventType.TOOL_COMPLETED,
                    run_id=state.run_id,
                    step_id=step_id,
                    phase=StepState.DISPATCHING_TOOL,
                    payload={
                        "tool_name": call.tool_name,
                        "status": str(ToolCallStatus.FAILED),
                        "blocked_dirty": True,
                    },
                )
                continue

            # Check for cached replay or dirty non-idempotent re-execution prevention
            if self.storage is not None and hasattr(self.storage, "get_replayable_cached_result"):
                try:
                    cached_res = self.storage.get_replayable_cached_result(
                        idempotency_key=idemp_key,
                        namespace_id=state.namespace_id,
                        tool_name=call.tool_name,
                        arguments=call.arguments,
                        argument_fingerprint=fingerprint,
                        run_id=state.run_id,
                        supports_provider_idempotency=supports_provider_idemp,
                        is_non_idempotent=is_non_idemp,
                    )
                except DirtyRollbackError as dre:
                    fail_details = FailureDetails(
                        error_type="DirtyRollbackError",
                        message=str(dre),
                        step_id=step_id,
                        error_code="PARTIAL_DIRTY_ROLLBACK",
                        recovery_hint="Manual operator intervention or compensation required",
                    )
                    blocked_res = ToolResult(
                        invocation_id=inv_id,
                        tool_name=call.tool_name,
                        status=ToolCallStatus.FAILED,
                        side_effect=side_effect,
                        idempotency_key=idemp_key,
                        error=str(dre),
                        failure_details=fail_details,
                    )
                    tool_results.append(blocked_res)
                    self.event_emitter.emit(
                        RuntimeEventType.TOOL_COMPLETED,
                        run_id=state.run_id,
                        step_id=step_id,
                        phase=StepState.DISPATCHING_TOOL,
                        payload={
                            "tool_name": call.tool_name,
                            "status": str(ToolCallStatus.FAILED),
                            "blocked_dirty": True,
                        },
                    )
                    continue

                if cached_res is not None:
                    tool_results.append(cached_res)
                    self.event_emitter.emit(
                        RuntimeEventType.TOOL_COMPLETED,
                        run_id=state.run_id,
                        step_id=step_id,
                        phase=StepState.DISPATCHING_TOOL,
                        payload={
                            "tool_name": call.tool_name,
                            "status": str(ToolCallStatus.COMPLETED),
                            "replayed": True,
                        },
                    )
                    continue

            if self.storage is not None:
                self.storage.record_tool_intent(
                    invocation_id=inv_id,
                    step_id=step_id,
                    namespace_id=state.namespace_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    side_effect=side_effect,
                    idempotency_key=idemp_key,
                    dispatched_at=dispatched_at,
                )

            if tool is None:
                fail_details = FailureDetails(
                    error_type="ToolNotFoundError",
                    message=f"Tool '{call.tool_name}' is not registered with runtime",
                    step_id=step_id,
                    error_code="TOOL_NOT_FOUND",
                )
                missing_res = ToolResult(
                    invocation_id=inv_id,
                    tool_name=call.tool_name,
                    status=ToolCallStatus.FAILED,
                    side_effect=SideEffectType.READ_ONLY,
                    idempotency_key=idemp_key,
                    error=f"Tool '{call.tool_name}' not found",
                    failure_details=fail_details,
                )
                tool_results.append(missing_res)
                if self.storage is not None:
                    self.storage.record_tool_result(missing_res, state.namespace_id)
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
                idempotency_key=idemp_key,
                deadline=deadline,
                cancellation_token=state.cancellation_token,
                business_operation_id=trusted_biz_id,
            )

            t_start = self.clock.now()
            try:
                raw_res = tool.execute(call.arguments, tool_ctx)
                t_elapsed_ms = (self.clock.now() - t_start).total_seconds() * 1000.0
                completed_res = ToolResult(
                    invocation_id=inv_id,
                    tool_name=call.tool_name,
                    status=ToolCallStatus.COMPLETED,
                    side_effect=tool.side_effect,
                    idempotency_key=idemp_key,
                    result=raw_res,
                    execution_time_ms=t_elapsed_ms,
                )
                tool_results.append(completed_res)
                if self.storage is not None:
                    self.storage.record_tool_result(completed_res, state.namespace_id)
            except (ExecutionCancelledError, PolicyViolationError) as exc:
                t_elapsed_ms = (self.clock.now() - t_start).total_seconds() * 1000.0
                is_mutating = tool.side_effect in (
                    SideEffectType.IDEMPOTENT_WRITE,
                    SideEffectType.NON_IDEMPOTENT_WRITE,
                )
                if is_mutating:
                    fail_details = FailureDetails(
                        error_type=type(exc).__name__,
                        message=str(exc),
                        step_id=step_id,
                        error_code="SIDE_EFFECT_UNCERTAIN",
                    )
                    uncertain_res = ToolResult(
                        invocation_id=inv_id,
                        tool_name=call.tool_name,
                        status=ToolCallStatus.OUTCOME_UNKNOWN,
                        side_effect=tool.side_effect,
                        idempotency_key=idemp_key,
                        error=str(exc),
                        failure_details=fail_details,
                        execution_time_ms=t_elapsed_ms,
                    )
                    tool_results.append(uncertain_res)
                    if self.storage is not None:
                        self.storage.record_tool_result(uncertain_res, state.namespace_id)
                    self.event_emitter.emit(
                        RuntimeEventType.TOOL_COMPLETED,
                        run_id=state.run_id,
                        step_id=step_id,
                        phase=StepState.DISPATCHING_TOOL,
                        payload={
                            "tool_name": call.tool_name,
                            "status": str(ToolCallStatus.OUTCOME_UNKNOWN),
                        },
                    )
                raise
            except Exception as exc:
                t_elapsed_ms = (self.clock.now() - t_start).total_seconds() * 1000.0
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
                err_res = ToolResult(
                    invocation_id=inv_id,
                    tool_name=call.tool_name,
                    status=status,
                    side_effect=tool.side_effect,
                    idempotency_key=idemp_key,
                    error=str(exc),
                    failure_details=fail_details,
                    execution_time_ms=t_elapsed_ms,
                )
                tool_results.append(err_res)
                if self.storage is not None:
                    self.storage.record_tool_result(err_res, state.namespace_id)

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
    ) -> CheckpointRecord | None:
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

        checkpoint: CheckpointRecord | None = None
        if self.storage is not None:
            state_payload: dict[str, Any] = {
                "run_id": str(state.run_id),
                "step_id": str(step_id),
                "step_number": state.step_count,
                "status": state.status.value,
                "tokens": state.cumulative_tokens.total_tokens,
            }
            if "working_memory_ids" in state.metadata:
                state_payload["working_memory_ids"] = list(state.metadata["working_memory_ids"])
            if "working_memory" in state.metadata:
                state_payload["working_memory"] = list(state.metadata["working_memory"])
            state_payload_json = json.dumps(state_payload, sort_keys=True)
            state_hash = compute_state_hash(state_payload)
            checkpoint = CheckpointRecord(
                checkpoint_id=CheckpointId(self.id_generator.generate_id(f"ckpt_{step_id}")),
                run_id=state.run_id,
                step_id=step_id,
                branch_id=DEFAULT_BRANCH_ID,
                namespace_id=state.namespace_id,
                parent_checkpoint_id=state.last_checkpoint_id,
                state_payload_json=state_payload_json,
                state_hash=state_hash,
                created_at=self.clock.now(),
            )
        return checkpoint

    def _phase_8_compile_outcome(
        self,
        state: LifecycleState,
        step_id: StepId,
        decision: ModelDecision,
        tool_results: list[ToolResult],
        step_compiler: StepOutcomeCompiler,
        checkpoint: CheckpointRecord | None = None,
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

        if self.storage is not None:
            current_run_snapshot = RunResult(
                run_id=state.run_id,
                namespace_id=state.namespace_id,
                status=state.status,
                step_count=state.step_count,
                total_tokens_consumed=state.cumulative_tokens.total_tokens,
                final_output=None,
                trace_id=f"trace_{state.run_id}",
                error=None,
                token_usage=state.cumulative_tokens,
                step_history=(*state.step_history, step_result),
                tool_results=tuple(tool_results),
            )
            if hasattr(self.storage, "commit_step_transaction"):
                self.storage.commit_step_transaction(
                    step_result=step_result,
                    run_result=current_run_snapshot,
                    namespace_id=state.namespace_id,
                    proposed_memories=(),
                    lineage_entries=(),
                    checkpoint=checkpoint,
                    trace_events=(),
                )
            else:
                self.storage.save_step(step_result, state.namespace_id)
                self.storage.save_run(current_run_snapshot)
                if checkpoint is not None:
                    self.storage.save_checkpoint(
                        checkpoint_id=checkpoint.checkpoint_id,
                        run_id=checkpoint.run_id,
                        step_id=checkpoint.step_id,
                        branch_id=checkpoint.branch_id,
                        namespace_id=checkpoint.namespace_id,
                        state_payload=checkpoint.state_payload(),
                        created_at=checkpoint.created_at,
                        parent_checkpoint_id=checkpoint.parent_checkpoint_id,
                    )
            if checkpoint is not None:
                state.last_checkpoint_id = checkpoint.checkpoint_id
        return step_result

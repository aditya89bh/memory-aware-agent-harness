"""Command-line interface and offline demonstration runner for AgentRuntime.

Enables executing bounded, deterministic agent tasks with lifecycle observation,
budget enforcement, tool execution, and structured result inspection.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from memory_aware_agent_harness.contracts import (
    ContextManifest,
    FailureDetails,
    MemoryId,
    MemoryKind,
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
    ToolProtocol,
)
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.events import (
    DiagnosticHook,
    RuntimeEvent,
    RuntimeEventType,
)
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter


class _DemoCalculatorTool(ToolProtocol):
    """Simple offline calculator tool for demonstration."""

    def __init__(self) -> None:
        self.name = "calculator"
        self.description = "Perform basic mathematical calculations"
        self.side_effect = SideEffectType.READ_ONLY

    def get_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["add", "multiply"]},
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["operation", "a", "b"],
        }

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        op = str(arguments.get("operation", "add"))
        a = float(arguments.get("a", 0.0))
        b = float(arguments.get("b", 0.0))
        if op == "add":
            return {"result": a + b}
        elif op == "multiply":
            return {"result": a * b}
        raise ValueError(f"Unknown operation: {op}")


class _VerboseConsoleLogger(DiagnosticHook):
    """Prints live lifecycle events to stderr as execution progresses."""

    def on_event(self, event: RuntimeEvent) -> None:
        match event.event_type:
            case RuntimeEventType.RUN_STARTED:
                task_str = str(event.payload.get("task", ""))
                sys.stderr.write(f"[*] RUN_STARTED: run_id={event.run_id} task='{task_str}'\n")
            case RuntimeEventType.STEP_STARTED:
                sys.stderr.write(
                    f"[*] STEP_STARTED: step_id={event.step_id} seq={event.sequence_number}\n"
                )
            case RuntimeEventType.PHASE_TRANSITION:
                sys.stderr.write(f"    -> Phase: {event.phase}\n")
            case RuntimeEventType.MODEL_INVOKED:
                sys.stderr.write("    -> Invoking model decision engine...\n")
            case RuntimeEventType.MODEL_RESPONDED:
                reason = event.payload.get("finish_reason")
                sys.stderr.write(f"    <- Model responded: finish_reason={reason}\n")
            case RuntimeEventType.TOOL_INVOKED:
                tool_name = event.payload.get("tool_name")
                sys.stderr.write(f"    -> Invoking tool: {tool_name}\n")
            case RuntimeEventType.TOOL_COMPLETED:
                tool_name = event.payload.get("tool_name")
                status = event.payload.get("status")
                sys.stderr.write(f"    <- Tool {tool_name} finished: status={status}\n")
            case RuntimeEventType.STEP_COMPLETED:
                sys.stderr.write(f"[*] STEP_COMPLETED: step_id={event.step_id}\n")
            case RuntimeEventType.RUN_TERMINATED:
                status = event.payload.get("status")
                steps = event.payload.get("step_count")
                tokens = event.payload.get("tokens_consumed")
                sys.stderr.write(
                    f"[*] RUN_TERMINATED: status={status} steps={steps} tokens={tokens}\n"
                )


def build_scenario_adapter(scenario: str) -> tuple[FakeModelAdapter, list[ToolProtocol]]:
    """Build pre-configured model adapter and tools for selected demonstration scenario."""
    calc_tool = _DemoCalculatorTool()

    if scenario == "simple":
        decision = ModelDecision(
            decision_id="dec_simple",
            step_id=StepId("step_0"),
            finish_reason=ModelFinishReason.STOP,
            content="Task completed successfully in single-step offline mode.",
            token_usage=TokenUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
        )
        return FakeModelAdapter(responses=[decision]), []

    elif scenario == "tool":
        step1 = ModelDecision(
            decision_id="dec_tool_call",
            step_id=StepId("step_0"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_calc",
                    tool_name="calculator",
                    arguments={"operation": "multiply", "a": 21.0, "b": 2.0},
                ),
            ),
            token_usage=TokenUsage(prompt_tokens=30, completion_tokens=20, total_tokens=50),
        )

        def _tool_observation_handler(
            manifest: ContextManifest,
            tools_schema: Sequence[Mapping[str, Any]],
            context: ModelCallContext,
        ) -> ModelDecision:
            # Parse observation dynamically from prompt feedback (P1-H1)
            prompt = manifest.compiled_prompt or ""
            observed_val: str | None = None
            tool_failed = False
            for line in prompt.splitlines():
                if "Observation (calculator" in line:
                    if ", failed)" in line or ", outcome_unknown)" in line:
                        tool_failed = True
                        break
                    try:
                        raw_json = line.split(":", 1)[-1].strip()
                        parsed = json.loads(raw_json)
                        if isinstance(parsed, dict) and "result" in parsed:
                            val = parsed["result"]
                            observed_val = str(int(val)) if int(val) == val else str(val)
                    except Exception:
                        tool_failed = True

            if tool_failed or observed_val is None:
                fail_details = FailureDetails(
                    error_type="ToolObservationError",
                    message="Calculator observation was not found or calculation failed",
                    step_id=context.step_id,
                    error_code="TOOL_OBSERVATION_FAILED",
                )
                return ModelDecision(
                    decision_id=f"dec_err_{context.step_id}",
                    step_id=context.step_id,
                    finish_reason=ModelFinishReason.ERROR,
                    content="Calculation failed: unable to obtain calculator tool observation.",
                    token_usage=TokenUsage(prompt_tokens=40, completion_tokens=15, total_tokens=55),
                    failure_details=fail_details,
                )

            return ModelDecision(
                decision_id=f"dec_final_{context.step_id}",
                step_id=context.step_id,
                finish_reason=ModelFinishReason.STOP,
                content=f"Calculation complete: 21 * 2 = {observed_val}.",
                token_usage=TokenUsage(prompt_tokens=40, completion_tokens=15, total_tokens=55),
            )

        adapter = FakeModelAdapter(
            responses=[step1],
            default_handler=_tool_observation_handler,
        )
        return adapter, [calc_tool]

    elif scenario == "budget":
        # Infinite step loop to trigger budget exhaustion guard
        decisions = [
            ModelDecision(
                decision_id=f"dec_loop_{i}",
                step_id=StepId(f"step_{i}"),
                finish_reason=ModelFinishReason.LENGTH,
                content=f"Continuing step {i}...",
                token_usage=TokenUsage(prompt_tokens=20, completion_tokens=20, total_tokens=40),
            )
            for i in range(10)
        ]
        return FakeModelAdapter(responses=decisions), []

    # Default fallback
    return FakeModelAdapter(), []


def handle_inspect_db(db_path: str, *, as_json: bool = False) -> int:
    """Inspect SQLite WAL database tables, counts, and integrity."""
    path = Path(db_path)
    if not path.exists():
        sys.stderr.write(f"Database file does not exist: {db_path}\n")
        return 1
    try:
        with SQLiteStorageAdapter(db_path) as storage:
            integrity_row = storage.conn.execute("PRAGMA integrity_check;").fetchone()
            integrity = integrity_row[0] if integrity_row else "unknown"
            runs_count = storage.conn.execute("SELECT COUNT(*) FROM runs;").fetchone()[0]
            steps_count = storage.conn.execute("SELECT COUNT(*) FROM run_steps;").fetchone()[0]
            tools_count = storage.conn.execute("SELECT COUNT(*) FROM tool_invocations;").fetchone()[
                0
            ]
            memories_total = storage.conn.execute(
                "SELECT COUNT(*) FROM memory_records;"
            ).fetchone()[0]
            memories_active = storage.conn.execute(
                "SELECT COUNT(*) FROM memory_records WHERE state = 'active';"
            ).fetchone()[0]
            tombstones_count = storage.conn.execute("SELECT COUNT(*) FROM tombstones;").fetchone()[
                0
            ]
            checkpoints_count = storage.conn.execute(
                "SELECT COUNT(*) FROM checkpoints;"
            ).fetchone()[0]
            trace_events_count = storage.conn.execute(
                "SELECT COUNT(*) FROM trace_events;"
            ).fetchone()[0]

            stats = {
                "database_path": str(path.resolve()),
                "integrity": integrity,
                "runs": runs_count,
                "steps": steps_count,
                "tool_invocations": tools_count,
                "memories_total": memories_total,
                "memories_active": memories_active,
                "tombstones": tombstones_count,
                "checkpoints": checkpoints_count,
                "trace_events": trace_events_count,
            }
            if as_json:
                sys.stdout.write(json.dumps(stats, indent=2) + "\n")
            else:
                sys.stdout.write("=" * 60 + "\n")
                sys.stdout.write(f"SQLite WAL Database Inspection: {path.name}\n")
                sys.stdout.write("=" * 60 + "\n")
                sys.stdout.write(f"Integrity Check:    {integrity}\n")
                sys.stdout.write(f"Runs:               {runs_count}\n")
                sys.stdout.write(f"Run Steps:          {steps_count}\n")
                sys.stdout.write(f"Tool Invocations:   {tools_count}\n")
                sys.stdout.write(
                    f"Total Memories:     {memories_total} (Active: {memories_active})\n"
                )
                sys.stdout.write(f"Tombstones:         {tombstones_count}\n")
                sys.stdout.write(f"Checkpoints:        {checkpoints_count}\n")
                sys.stdout.write(f"Trace Events:       {trace_events_count}\n")
                sys.stdout.write("=" * 60 + "\n")
            return 0
    except Exception as exc:
        sys.stderr.write(f"Error inspecting database: {exc}\n")
        return 1


def handle_query_memories(
    db_path: str,
    *,
    namespace: str,
    kind: str | None = None,
    query: str | None = None,
    limit: int = 50,
    as_json: bool = False,
) -> int:
    """Query and filter active memories from durable storage."""
    path = Path(db_path)
    if not path.exists():
        sys.stderr.write(f"Database file does not exist: {db_path}\n")
        return 1
    ns = NamespaceId(namespace)
    mem_kind: MemoryKind | None = None
    if kind is not None:
        try:
            mem_kind = MemoryKind(kind.lower())
        except ValueError:
            sys.stderr.write(f"Invalid memory kind: {kind}\n")
            return 1

    try:
        with SQLiteStorageAdapter(db_path) as storage:
            if query is not None and query.strip():
                kinds_filter = (mem_kind,) if mem_kind is not None else ()
                records = list(
                    storage.search(
                        namespace_id=ns,
                        query=query.strip(),
                        kinds=kinds_filter,
                        limit=limit,
                    )
                )
            elif mem_kind is not None:
                records = list(storage.list_active(ns, kind=mem_kind))[:limit]
            else:
                records = []
                for k in MemoryKind:
                    records.extend(storage.list_active(ns, kind=k))
                records = records[:limit]

            if as_json:
                data = [rec.to_dict() for rec in records]
                sys.stdout.write(json.dumps(data, indent=2) + "\n")
            else:
                sys.stdout.write(f"Memories in namespace '{namespace}' (Count: {len(records)}):\n")
                for rec in records:
                    content_preview = rec.content.replace("\n", " ")
                    if len(content_preview) > 60:
                        content_preview = content_preview[:57] + "..."
                    sys.stdout.write(
                        f"  [{rec.kind.value.upper()}] {rec.id} (conf={rec.confidence:.2f}): "
                        f"{content_preview}\n"
                    )
            return 0
    except Exception as exc:
        sys.stderr.write(f"Error querying memories: {exc}\n")
        return 1


def handle_tombstone_memory(
    db_path: str,
    *,
    namespace: str,
    memory_id: str,
    reason: str,
    as_json: bool = False,
) -> int:
    """Tombstone (soft-delete) a memory record with verifiable audit reason."""
    path = Path(db_path)
    if not path.exists():
        sys.stderr.write(f"Database file does not exist: {db_path}\n")
        return 1
    ns = NamespaceId(namespace)
    mem_id = MemoryId(memory_id)
    try:
        with SQLiteStorageAdapter(db_path) as storage:
            storage.tombstone(mem_id, ns, reason=reason)
            if as_json:
                res = {
                    "status": "tombstoned",
                    "memory_id": str(mem_id),
                    "namespace_id": str(ns),
                    "reason": reason,
                }
                sys.stdout.write(json.dumps(res, indent=2) + "\n")
            else:
                sys.stdout.write(
                    f"Successfully tombstoned memory '{mem_id}' in namespace '{ns}'.\n"
                    f"Reason: {reason}\n"
                )
            return 0
    except Exception as exc:
        sys.stderr.write(f"Error tombstoning memory: {exc}\n")
        return 1


def handle_recover_runs(
    db_path: str,
    *,
    namespace: str,
    as_json: bool = False,
) -> int:
    """Scan and reconcile orphaned runs in storage after a process crash."""
    path = Path(db_path)
    if not path.exists():
        sys.stderr.write(f"Database file does not exist: {db_path}\n")
        return 1
    ns = NamespaceId(namespace)
    try:
        with SQLiteStorageAdapter(db_path) as storage:
            reports = storage.reconcile_all_orphaned_runs(ns)
            if as_json:
                data = [rep.to_dict() for rep in reports]
                sys.stdout.write(json.dumps(data, indent=2) + "\n")
            else:
                sys.stdout.write(
                    f"Reconciled {len(reports)} orphaned run(s) in namespace '{ns}'.\n"
                )
                for rep in reports:
                    dirty_str = " (DIRTY ROLLBACK)" if rep.is_dirty else ""
                    sys.stdout.write(
                        f"  - Run {rep.run_id}: {rep.previous_status.value} -> "
                        f"{rep.new_status.value}{dirty_str} ({rep.reason})\n"
                    )
            return 0
    except Exception as exc:
        sys.stderr.write(f"Error recovering orphaned runs: {exc}\n")
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint executing agent runs or inspecting durable storage."""
    args_list = list(sys.argv[1:] if argv is None else argv)

    if args_list:
        subcmd = args_list[0]
        if subcmd == "inspect-db":
            sub_parser = argparse.ArgumentParser(
                prog="memory-agent inspect-db",
                description="Inspect SQLite WAL database tables, counts, and integrity.",
            )
            sub_parser.add_argument("db_path", help="Path to SQLite database file.")
            sub_parser.add_argument(
                "--json", action="store_true", help="Output summary in JSON format."
            )
            sub_args = sub_parser.parse_args(args_list[1:])
            return handle_inspect_db(sub_args.db_path, as_json=sub_args.json)

        if subcmd == "query-memories":
            sub_parser = argparse.ArgumentParser(
                prog="memory-agent query-memories",
                description="Query and filter active memories from durable storage.",
            )
            sub_parser.add_argument("db_path", help="Path to SQLite database file.")
            sub_parser.add_argument(
                "--namespace", required=True, help="Tenant namespace identifier."
            )
            sub_parser.add_argument(
                "--kind",
                choices=["working", "episodic", "semantic", "procedural"],
                default=None,
                help="Optional memory category filter.",
            )
            sub_parser.add_argument("--query", default=None, help="Optional search query text.")
            sub_parser.add_argument(
                "--limit",
                type=int,
                default=50,
                help="Maximum memories to return (default: 50).",
            )
            sub_parser.add_argument(
                "--json", action="store_true", help="Output results in JSON format."
            )
            sub_args = sub_parser.parse_args(args_list[1:])
            return handle_query_memories(
                sub_args.db_path,
                namespace=sub_args.namespace,
                kind=sub_args.kind,
                query=sub_args.query,
                limit=sub_args.limit,
                as_json=sub_args.json,
            )

        if subcmd == "tombstone-memory":
            sub_parser = argparse.ArgumentParser(
                prog="memory-agent tombstone-memory",
                description="Tombstone (soft-delete) a memory record with verifiable audit reason.",
            )
            sub_parser.add_argument("db_path", help="Path to SQLite database file.")
            sub_parser.add_argument(
                "--namespace", required=True, help="Tenant namespace identifier."
            )
            sub_parser.add_argument("--memory-id", required=True, help="Target memory identifier.")
            sub_parser.add_argument("--reason", required=True, help="Audit reason for tombstoning.")
            sub_parser.add_argument(
                "--json", action="store_true", help="Output result in JSON format."
            )
            sub_args = sub_parser.parse_args(args_list[1:])
            return handle_tombstone_memory(
                sub_args.db_path,
                namespace=sub_args.namespace,
                memory_id=sub_args.memory_id,
                reason=sub_args.reason,
                as_json=sub_args.json,
            )

        if subcmd == "recover-runs":
            sub_parser = argparse.ArgumentParser(
                prog="memory-agent recover-runs",
                description="Scan and reconcile orphaned runs in storage after process crash.",
            )
            sub_parser.add_argument("db_path", help="Path to SQLite database file.")
            sub_parser.add_argument(
                "--namespace", required=True, help="Tenant namespace identifier."
            )
            sub_parser.add_argument(
                "--json", action="store_true", help="Output report in JSON format."
            )
            sub_args = sub_parser.parse_args(args_list[1:])
            return handle_recover_runs(
                sub_args.db_path,
                namespace=sub_args.namespace,
                as_json=sub_args.json,
            )

        if subcmd == "run":
            args_list = args_list[1:]

    parser = argparse.ArgumentParser(
        prog="memory-agent",
        description="Memory-Aware Agent Harness - Offline Demo & Runtime Controller",
    )
    parser.add_argument(
        "task",
        nargs="?",
        default=None,
        help="Instruction or query for the agent runtime to execute.",
    )
    parser.add_argument(
        "--scenario",
        choices=["simple", "tool", "budget"],
        default="simple",
        help="Pre-configured demonstration scenario (simple, tool, budget).",
    )
    parser.add_argument(
        "--namespace",
        default="demo_ns",
        help="Tenant or memory namespace identifier (default: demo_ns).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=5,
        help="Maximum allowed steps before budget exhaustion (default: 5).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1000,
        help="Maximum cumulative token budget (default: 1000).",
    )
    parser.add_argument(
        "--max-time",
        type=float,
        default=60.0,
        help="Maximum execution wall-clock time in seconds (default: 60.0).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print live lifecycle diagnostic events during execution.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output terminal RunResult in canonical JSON format.",
    )

    args = parser.parse_args(args_list)

    task_desc = args.task
    if not task_desc:
        match args.scenario:
            case "simple":
                task_desc = "Demonstrate basic single-step completion"
            case "tool":
                task_desc = "Calculate 21 multiplied by 2 using tool"
            case "budget":
                task_desc = "Demonstrate step budget exhaustion guard"

    model_adapter, tools = build_scenario_adapter(args.scenario)

    listeners: list[DiagnosticHook] = []
    if args.verbose:
        listeners.append(_VerboseConsoleLogger())

    budgets = RunBudgets(
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        max_time_seconds=args.max_time,
    )

    runtime = AgentRuntime(
        model=model_adapter,
        tools=tools,
        budgets=budgets,
        event_listeners=listeners,
    )

    try:
        result = runtime.run(
            task_desc,
            namespace_id=NamespaceId(args.namespace),
        )
    except Exception as exc:
        sys.stderr.write(f"Error executing agent runtime: {exc}\n")
        return 1

    if args.json:
        sys.stdout.write(result.to_json() + "\n")
    else:
        sys.stdout.write("=" * 60 + "\n")
        sys.stdout.write("Agent Runtime Execution Result\n")
        sys.stdout.write("=" * 60 + "\n")
        sys.stdout.write(f"Run ID:            {result.run_id}\n")
        sys.stdout.write(f"Namespace:         {result.namespace_id}\n")
        sys.stdout.write(f"Status:            {result.status.value.upper()}\n")
        sys.stdout.write(f"Steps Executed:    {result.step_count}\n")
        sys.stdout.write(f"Tokens Consumed:   {result.total_tokens_consumed}\n")
        if result.final_output:
            sys.stdout.write(f"Final Output:      {result.final_output}\n")
        if result.failure_details:
            sys.stdout.write(f"Failure Code:      {result.failure_details.error_code}\n")
            sys.stdout.write(f"Failure Reason:    {result.failure_details.message}\n")
        sys.stdout.write("=" * 60 + "\n")

    match result.status:
        case RunStatus.COMPLETED:
            return 0
        case RunStatus.BUDGET_EXHAUSTED:
            return 2
        case RunStatus.CANCELLED:
            return 130
        case _:
            return 1


if __name__ == "__main__":
    sys.exit(main())

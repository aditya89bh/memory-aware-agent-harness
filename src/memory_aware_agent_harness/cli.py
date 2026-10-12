"""Command-line interface and offline demonstration runner for AgentRuntime.

Enables executing bounded, deterministic agent tasks with lifecycle observation,
budget enforcement, tool execution, and structured result inspection.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from memory_aware_agent_harness.contracts import (
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
        step2 = ModelDecision(
            decision_id="dec_final",
            step_id=StepId("step_1"),
            finish_reason=ModelFinishReason.STOP,
            content="Calculation complete: 21 * 2 = 42.",
            token_usage=TokenUsage(prompt_tokens=40, completion_tokens=15, total_tokens=55),
        )
        return FakeModelAdapter(responses=[step1, step2]), [calc_tool]

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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint executing a bounded offline agent run."""
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

    args = parser.parse_args(argv)

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

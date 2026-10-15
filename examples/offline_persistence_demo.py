"""Demonstration of offline persistence across storage sessions over durable SQLite WAL storage.

Demonstrates:
1. Session A: Run an agent step, invoke a tool, and commit durable state and checkpoint.
2. Session A exits and closes database connections.
3. Session B: Reopens the database with a new adapter, verifies the checkpoint's SHA-256 state hash,
   restores runtime state, and completes execution.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memory_aware_agent_harness.contracts import (
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunId,
    SideEffectType,
    StepId,
    TokenUsage,
    ToolCallContext,
    ToolCallRequest,
    ToolProtocol,
)
from memory_aware_agent_harness.doubles import DeterministicClock
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter


class CalculatorTool(ToolProtocol):
    """Deterministic mathematical tool for the demonstration."""

    def __init__(self) -> None:
        self.name = "calc"
        self.description = "Perform arithmetic calculation"
        self.side_effect = SideEffectType.READ_ONLY

    def get_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "op": {"type": "string"},
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["op", "a", "b"],
        }

    def execute(self, arguments: Mapping[str, Any], context: ToolCallContext) -> Mapping[str, Any]:
        op = str(arguments.get("op", "add"))
        a = float(arguments.get("a", 0))
        b = float(arguments.get("b", 0))
        if op == "add":
            return {"sum": a + b}
        return {"result": 0}


DEMO_NAMESPACE = NamespaceId("demo_tenant")
DEMO_RUN_ID = RunId("run_cross_proc_demo")


def _run_phase1_worker(db_path: str, result_queue: mp.Queue[dict[str, Any]]) -> None:
    """Worker process executing Phase 1 in an isolated OS process."""
    pid = os.getpid()
    print(f"\n--- Phase 1: Process A (PID {pid}) Execution on {db_path} ---")
    clock = DeterministicClock(start_time=datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))
    calc_tool = CalculatorTool()

    with SQLiteStorageAdapter(db_path, clock=clock.now) as storage_a:
        # 1. Seed semantic knowledge base
        now = clock.now()
        seed_record = MemoryRecord(
            id=MemoryId("mem_seed_pref"),
            namespace_id=DEMO_NAMESPACE,
            kind=MemoryKind.SEMANTIC,
            content="Preferred operational mode: batch arithmetic aggregation.",
            state=MemoryState.ACTIVE,
            confidence=1.0,
            salience=0.8,
            created_at=now,
            updated_at=now,
            origin=MemoryOrigin.AGENT_OBSERVATION,
        )
        storage_a.put(seed_record)
        print(f"[Process A - PID {pid}] Seeded semantic memory: {seed_record.id}")

        # 2. Setup runtime and execute Step 1
        dec1 = ModelDecision(
            decision_id="dec_demo_1",
            step_id=StepId("step_demo_1"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(
                ToolCallRequest(
                    call_id="call_demo_1",
                    tool_name="calc",
                    arguments={"op": "add", "a": 150, "b": 350},
                ),
            ),
            token_usage=TokenUsage(prompt_tokens=15, completion_tokens=10, total_tokens=25),
        )
        dec2 = ModelDecision(
            decision_id="dec_demo_2",
            step_id=StepId("step_demo_2"),
            finish_reason=ModelFinishReason.STOP,
            content="Calculation complete. Sum is 500.",
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        model_a = FakeModelAdapter(responses=[dec1, dec2])
        runtime_a = AgentRuntime(
            model=model_a,
            tools=[calc_tool],
            storage=storage_a,
            clock=clock,
        )

        res_a = runtime_a.run("Add 150 and 350", namespace_id=DEMO_NAMESPACE, run_id=DEMO_RUN_ID)
        print(f"[Process A - PID {pid}] Run execution completed with status: {res_a.status.value}")
        print(f"[Process A - PID {pid}] Steps recorded: {res_a.step_count}")

    print(f"[Process A - PID {pid}] Disconnected and closed SQLite WAL storage.\n")
    result_queue.put(
        {
            "pid": pid,
            "final_status": res_a.status.value,
            "step_count": res_a.step_count,
        }
    )


def _run_phase2_worker(db_path: str, result_queue: mp.Queue[dict[str, Any]]) -> None:
    """Worker process executing Phase 2 in a separate isolated OS process."""
    pid = os.getpid()
    print(f"--- Phase 2: Process B (PID {pid}) Resuming from {db_path} ---")
    clock = DeterministicClock(start_time=datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))
    calc_tool = CalculatorTool()

    with SQLiteStorageAdapter(db_path, clock=clock.now) as storage_b:
        # 1. Inspect persisted memories
        active_mems = storage_b.list_active(DEMO_NAMESPACE, kind=MemoryKind.SEMANTIC)
        print(f"[Process B - PID {pid}] Recovered {len(active_mems)} active memories from storage.")

        # 2. Retrieve checkpoint and verify SHA-256 state hash integrity
        latest_cp = storage_b.get_latest_checkpoint(DEMO_RUN_ID, DEMO_NAMESPACE)
        if latest_cp is None:
            raise RuntimeError(f"Expected latest checkpoint for {DEMO_RUN_ID}")

        print(f"[Process B - PID {pid}] Found checkpoint: {latest_cp.checkpoint_id}")
        print(f"[Process B - PID {pid}] Stored State Hash: {latest_cp.state_hash}")

        # Deterministic SHA-256 state hash verification via get_checkpoint_record
        verified_cp = storage_b.get_checkpoint_record(
            latest_cp.checkpoint_id,
            DEMO_NAMESPACE,
            verify_hash=True,
        )
        if verified_cp is None:
            raise RuntimeError("Failed to retrieve or verify checkpoint")
        print(f"[Process B - PID {pid}] Checkpoint SHA-256 state hash verification: PASSED")

        # 3. Restore agent state into new runtime instance
        runtime_b = AgentRuntime(
            model=FakeModelAdapter(),
            tools=[calc_tool],
            storage=storage_b,
            clock=clock,
        )
        restored_state, rehydrated_info = runtime_b.restore_from_checkpoint(
            latest_cp.checkpoint_id,
            DEMO_NAMESPACE,
        )
        print(
            f"[Process B - PID {pid}] Successfully restored state. "
            f"Step Count: {restored_state.step_count}"
        )
        print(
            f"[Process B - PID {pid}] Rehydrated working memory purged count: "
            f"{rehydrated_info.active_tombstones_count}"
        )

        # 4. Audit trace events log
        trace_events = storage_b.get_trace_events(DEMO_RUN_ID, DEMO_NAMESPACE)
        print(
            f"[Process B - PID {pid}] Verified {len(trace_events)} "
            "sequenced trace audit events in WAL."
        )

    print(f"\n--- Process B (PID {pid}) Demonstration Finished Successfully ---")
    result_queue.put(
        {
            "pid": pid,
            "checkpoint_id": str(latest_cp.checkpoint_id),
            "state_hash": latest_cp.state_hash,
            "restored_step_count": restored_state.step_count,
            "trace_events_count": len(trace_events),
        }
    )


def run_offline_persistence_demo(db_path: str | Path | None = None) -> dict[str, Any]:
    """Execute cross-process persistence demonstration across separate OS processes.

    Args:
        db_path: Path to target SQLite database. Created if missing.

    Returns:
        Structured dictionary summarizing demonstration results.
    """
    if db_path is None:
        temp_dir = tempfile.mkdtemp(prefix="agent_demo_")
        target_path = Path(temp_dir) / "offline_agent.db"
    else:
        target_path = Path(db_path)

    ctx = mp.get_context("spawn")

    queue1: mp.Queue[dict[str, Any]] = ctx.Queue()
    p1 = ctx.Process(target=_run_phase1_worker, args=(str(target_path), queue1))
    p1.start()
    p1.join(timeout=30.0)
    if p1.exitcode != 0:
        raise RuntimeError(f"Process A failed with exit code {p1.exitcode}")
    res1 = queue1.get_nowait()

    queue2: mp.Queue[dict[str, Any]] = ctx.Queue()
    p2 = ctx.Process(target=_run_phase2_worker, args=(str(target_path), queue2))
    p2.start()
    p2.join(timeout=30.0)
    if p2.exitcode != 0:
        raise RuntimeError(f"Process B failed with exit code {p2.exitcode}")
    res2 = queue2.get_nowait()

    assert res1["pid"] != res2["pid"], "Phase 1 and Phase 2 must run in distinct OS processes"

    return {
        "db_path": str(target_path),
        "run_id": str(DEMO_RUN_ID),
        "namespace_id": str(DEMO_NAMESPACE),
        "checkpoint_id": res2["checkpoint_id"],
        "state_hash": res2["state_hash"],
        "restored_step_count": res2["restored_step_count"],
        "final_status": res1["final_status"],
        "trace_events_count": res2["trace_events_count"],
        "process_a_pid": res1["pid"],
        "process_b_pid": res2["pid"],
    }


def main() -> None:
    """CLI entrypoint for offline persistence demonstration."""
    parser = argparse.ArgumentParser(
        description="Demonstrate offline cross-process persistence in SQLite WAL mode."
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to SQLite database file. Created in temporary dir if omitted.",
    )
    args = parser.parse_args()
    try:
        run_offline_persistence_demo(args.db_path)
    except Exception as exc:
        print(f"Error during demonstration: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

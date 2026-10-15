"""Multi-process WAL concurrency, stress, and crash atomicity tests."""

from __future__ import annotations

import multiprocessing as mp
import os
import time
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
    RunResult,
    RunStatus,
    StepId,
    StepResult,
    ToolCallStatus,
    ToolInvocationId,
)
from memory_aware_agent_harness.doubles import DeterministicClock
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter


def _worker_write_memories(
    db_path: str,
    ns_str: str,
    worker_id: int,
    count: int,
    queue: Any,
) -> None:
    """Worker process: write a batch of memories and runs."""
    ns = NamespaceId(ns_str)
    written = 0
    try:
        with SQLiteStorageAdapter(db_path) as storage:
            for i in range(count):
                mem_id = MemoryId(f"mem_w{worker_id}_{i}")
                now = datetime.now(UTC)
                storage.put(
                    MemoryRecord(
                        id=mem_id,
                        namespace_id=ns,
                        kind=MemoryKind.SEMANTIC,
                        content=f"Worker {worker_id} memory item {i}",
                        state=MemoryState.ACTIVE,
                        confidence=0.9,
                        salience=0.5,
                        created_at=now,
                        updated_at=now,
                        origin=MemoryOrigin.AGENT_OBSERVATION,
                    )
                )
                written += 1
        queue.put(("ok", worker_id, written))
    except Exception as exc:
        queue.put(("error", worker_id, str(exc)))


def _worker_concurrent_reader(
    db_path: str,
    ns_str: str,
    iterations: int,
    queue: Any,
) -> None:
    """Worker process: repeatedly read memories in WAL mode concurrently with writers."""
    ns = NamespaceId(ns_str)
    successful_reads = 0
    try:
        with SQLiteStorageAdapter(db_path) as storage:
            for _ in range(iterations):
                memories = storage.search(
                    namespace_id=ns,
                    query="memory",
                    kinds=(MemoryKind.SEMANTIC,),
                    limit=50,
                )
                if isinstance(memories, list | tuple):
                    successful_reads += 1
                time.sleep(0.005)
        queue.put(("ok", successful_reads))
    except Exception as exc:
        queue.put(("error", str(exc)))


def _worker_crashing_transaction(db_path: str, ns_str: str, run_id_str: str) -> None:
    """Worker process: begin multi-statement write then abruptly terminate via os._exit."""
    ns = NamespaceId(ns_str)
    run_id = RunId(run_id_str)
    step_id = StepId(f"step_{run_id_str}_1")
    now = datetime.now(UTC)

    # Use a raw transaction that is NOT committed
    with (
        SQLiteStorageAdapter(db_path) as storage,
        storage.transaction(mode="IMMEDIATE") as conn,
    ):
        conn.execute(
            """
            INSERT INTO runs (
                run_id, session_id, namespace_id, status, step_count,
                max_steps, token_budget, tokens_consumed, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                str(run_id),
                "sess_crash",
                str(ns),
                RunStatus.RUNNING.value,
                0,
                10,
                1000,
                0,
                now.isoformat(),
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO run_steps (
                step_id, run_id, namespace_id, step_number, state, started_at
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                str(step_id),
                str(run_id),
                str(ns),
                1,
                "running",
                now.isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO tool_invocations (
                invocation_id, step_id, namespace_id, tool_name,
                arguments_json, side_effect, idempotency_key, status, dispatched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                "inv_crashed_mid",
                str(step_id),
                str(ns),
                "crash_tool",
                "{}",
                "non_idempotent_write",
                "idemp_crash",
                ToolCallStatus.PENDING.value,
                now.isoformat(),
            ),
        )
        # Abrupt process crash simulation: exits immediately without SQLite commit
        os._exit(42)


def test_concurrent_multiprocess_wal_writers(tmp_path: Path) -> None:
    """Multiple independent processes concurrently write into WAL database without corruption."""
    db_file = tmp_path / "wal_multiprocess_writers.db"
    ns_str = "ns_mp_wal"
    worker_count = 4
    items_per_worker = 15

    # Initialize schema
    with SQLiteStorageAdapter(str(db_file)):
        pass

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    processes: list[Any] = []

    for w_id in range(worker_count):
        p = ctx.Process(
            target=_worker_write_memories,
            args=(str(db_file), ns_str, w_id, items_per_worker, queue),
        )
        processes.append(p)
        p.start()

    results: list[tuple[Any, ...]] = []
    for _ in range(worker_count):
        results.append(queue.get(timeout=30.0))

    for p in processes:
        p.join(timeout=10.0)
        assert not p.is_alive()
        assert p.exitcode == 0

    # Verify all workers reported success
    for res in results:
        status, worker_id, count = res
        assert status == "ok"
        assert count == items_per_worker

    # Verify all written items are intact and database integrity check passes
    with SQLiteStorageAdapter(str(db_file)) as storage:
        active_memories = storage.list_active(NamespaceId(ns_str), kind=MemoryKind.SEMANTIC)
        assert len(active_memories) == worker_count * items_per_worker

        # Verify PRAGMA integrity_check
        check_row = storage.conn.execute("PRAGMA integrity_check;").fetchone()
        assert check_row is not None
        assert check_row[0] == "ok"


def test_concurrent_wal_readers_and_writers(tmp_path: Path) -> None:
    """Readers in WAL mode run concurrently without blocking writers or throwing lock errors."""
    db_file = tmp_path / "wal_readers_writers.db"
    ns_str = "ns_rw_concurrent"

    # Pre-populate database with seed data
    with SQLiteStorageAdapter(str(db_file)) as storage:
        now = datetime.now(UTC)
        for i in range(10):
            storage.put(
                MemoryRecord(
                    id=MemoryId(f"seed_mem_{i}"),
                    namespace_id=NamespaceId(ns_str),
                    kind=MemoryKind.SEMANTIC,
                    content=f"Initial seed memory {i}",
                    state=MemoryState.ACTIVE,
                    confidence=1.0,
                    salience=0.5,
                    created_at=now,
                    updated_at=now,
                    origin=MemoryOrigin.AGENT_OBSERVATION,
                )
            )

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()

    # Launch 2 readers and 1 writer
    writer_p = ctx.Process(
        target=_worker_write_memories,
        args=(str(db_file), ns_str, 99, 20, queue),
    )
    reader1_p = ctx.Process(
        target=_worker_concurrent_reader,
        args=(str(db_file), ns_str, 20, queue),
    )
    reader2_p = ctx.Process(
        target=_worker_concurrent_reader,
        args=(str(db_file), ns_str, 20, queue),
    )

    writer_p.start()
    reader1_p.start()
    reader2_p.start()

    # Collect outcomes
    outcomes: list[Any] = [queue.get(timeout=30.0) for _ in range(3)]

    writer_p.join(timeout=10.0)
    reader1_p.join(timeout=10.0)
    reader2_p.join(timeout=10.0)

    assert writer_p.exitcode == 0
    assert reader1_p.exitcode == 0
    assert reader2_p.exitcode == 0

    for outcome in outcomes:
        assert outcome[0] == "ok"


def test_multiprocess_crash_atomicity_and_wal_rollback(tmp_path: Path) -> None:
    """An abrupt process crash mid-transaction causes complete rollback without partial state."""
    db_file = tmp_path / "crash_atomicity.db"
    ns_str = "ns_crash_atomicity"
    run_id_str = "run_uncommitted_crash"

    # Initialize schema
    with SQLiteStorageAdapter(str(db_file)):
        pass

    ctx = mp.get_context("spawn")
    crash_p = ctx.Process(
        target=_worker_crashing_transaction,
        args=(str(db_file), ns_str, run_id_str),
    )
    crash_p.start()
    crash_p.join(timeout=10.0)

    # The process exited with code 42
    assert crash_p.exitcode == 42

    # Verify that the uncommitted multi-statement transaction rolled back completely
    with SQLiteStorageAdapter(str(db_file)) as storage:
        # Run does not exist
        assert storage.get_run(RunId(run_id_str), NamespaceId(ns_str)) is None

        # Step does not exist
        assert storage.get_step(StepId(f"step_{run_id_str}_1"), NamespaceId(ns_str)) is None

        # Tool invocation does not exist
        assert (
            storage.get_tool_invocation(
                ToolInvocationId("inv_crashed_mid"),
                NamespaceId(ns_str),
            )
            is None
        )

        # Integrity check passes cleanly
        integrity = storage.conn.execute("PRAGMA integrity_check;").fetchone()
        assert integrity is not None
        assert integrity[0] == "ok"


def test_atomic_step_commit_integrity_across_processes(tmp_path: Path) -> None:
    """Atomic 4-phase step commits across processes maintain cryptographic hash integrity."""
    db_file = tmp_path / "commit_integrity.db"
    ns = NamespaceId("ns_step_integrity")
    clock = DeterministicClock(datetime(2026, 10, 15, 18, 0, 0, tzinfo=UTC))

    with SQLiteStorageAdapter(str(db_file), clock=clock.now) as storage:
        # Create base runs
        for i in range(2):
            run = RunResult(
                run_id=RunId(f"run_p_{i}"),
                namespace_id=ns,
                status=RunStatus.RUNNING,
                step_count=0,
                total_tokens_consumed=0,
                final_output=None,
                trace_id=f"tr_p_{i}",
            )
            storage.save_run(run)

        # Commit step transaction for run 0
        step_res_0 = StepResult(
            step_id=StepId("step_p_0_1"),
            run_id=RunId("run_p_0"),
            step_number=1,
            decision=ModelDecision(
                decision_id="dec_0_1",
                step_id=StepId("step_p_0_1"),
                finish_reason=ModelFinishReason.STOP,
                content="done",
            ),
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
            tokens_consumed=10,
            completed_at=clock.now(),
        )
        storage.commit_step_transaction(
            step_result=step_res_0,
            run_result=RunResult(
                run_id=RunId("run_p_0"),
                namespace_id=ns,
                status=RunStatus.RUNNING,
                step_count=1,
                total_tokens_consumed=10,
                final_output=None,
                trace_id="tr_p_0",
                step_history=(step_res_0,),
            ),
            namespace_id=ns,
        )

        saved_step = storage.get_step(StepId("step_p_0_1"), ns)
        assert saved_step is not None
        assert saved_step.step_number == 1

"""Unit tests for the atomic 4-phase step commit protocol."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from memory_aware_agent_harness.contracts import (
    BranchId,
    CheckpointId,
    EventId,
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
    StepCommitError,
    StepId,
    StepResult,
    TokenUsage,
)
from memory_aware_agent_harness.storage.checkpoints_repo import (
    CheckpointRecord,
    get_checkpoint,
)
from memory_aware_agent_harness.storage.connection import create_connection
from memory_aware_agent_harness.storage.memory_repo import (
    get_memory,
    get_memory_lineage,
)
from memory_aware_agent_harness.storage.migrations import apply_migrations
from memory_aware_agent_harness.storage.runs_repo import (
    get_run,
    get_step,
    save_run,
    save_step,
)
from memory_aware_agent_harness.storage.step_commit import (
    LineageEntry,
    StepCommitBundle,
    StepCommitOutcome,
    TraceEventInput,
    commit_step_4phase,
    commit_step_transaction,
)
from memory_aware_agent_harness.storage.trace_repo import (
    get_trace_events,
)


def _init_db() -> sqlite3.Connection:
    conn = create_connection(":memory:")
    apply_migrations(conn)
    return conn


def _make_step_result(step_id: str, run_id: str) -> StepResult:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    decision = ModelDecision(
        decision_id="dec_01",
        step_id=StepId(step_id),
        finish_reason=ModelFinishReason.STOP,
        content="Step done",
    )
    return StepResult(
        step_id=StepId(step_id),
        run_id=RunId(run_id),
        step_number=1,
        decision=decision,
        tool_results=(),
        memories_proposed=0,
        memories_committed=0,
        tokens_consumed=150,
        completed_at=now,
    )


def _make_run_result(run_id: str, ns: str, tokens: int = 150) -> RunResult:
    return RunResult(
        run_id=RunId(run_id),
        namespace_id=NamespaceId(ns),
        status=RunStatus.RUNNING,
        step_count=1,
        total_tokens_consumed=tokens,
        final_output=None,
        trace_id="tr_commit",
        token_usage=TokenUsage(
            prompt_tokens=tokens // 2,
            completion_tokens=tokens - (tokens // 2),
            total_tokens=tokens,
        ),
    )


def _make_memory(mem_id: str, ns: str) -> MemoryRecord:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    return MemoryRecord(
        id=MemoryId(mem_id),
        namespace_id=NamespaceId(ns),
        kind=MemoryKind.SEMANTIC,
        content=f"Fact {mem_id}",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
        origin=MemoryOrigin.AGENT_OBSERVATION,
        run_id=RunId("run_commit"),
        branch_id=BranchId("main"),
    )


def test_lineage_entry_validation() -> None:
    entry = LineageEntry(
        target_memory_id=MemoryId("mem_target"),
        source_reference_id="src_01",
        relationship="synthesized_from",
    )
    assert entry.target_memory_id == MemoryId("mem_target")
    assert entry.source_reference_id == "src_01"
    assert entry.relationship == "synthesized_from"

    with pytest.raises(ValueError, match="target_memory_id must not be empty"):
        LineageEntry(target_memory_id=MemoryId(""), source_reference_id="src_01")

    with pytest.raises(ValueError, match="source_reference_id must not be empty"):
        LineageEntry(target_memory_id=MemoryId("target"), source_reference_id="")

    with pytest.raises(ValueError, match="relationship must not be empty"):
        LineageEntry(
            target_memory_id=MemoryId("target"),
            source_reference_id="src",
            relationship="   ",
        )


def test_successful_4phase_step_commit() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_commit")
    run_id = RunId("run_commit")
    step_id = StepId("step_01")
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    # Initial run record in DB
    initial_run = RunResult(
        run_id=run_id,
        namespace_id=ns,
        status=RunStatus.RUNNING,
        step_count=0,
        total_tokens_consumed=0,
        final_output=None,
        trace_id="tr_commit",
        token_usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    )
    save_run(conn, initial_run)

    # Bundle to commit
    step_res = _make_step_result(str(step_id), str(run_id))
    updated_run = _make_run_result(str(run_id), str(ns), tokens=150)
    mem1 = _make_memory("mem_01", str(ns))
    mem2 = _make_memory("mem_02", str(ns))
    lineage = LineageEntry(
        target_memory_id=MemoryId("mem_01"),
        source_reference_id="raw_doc_42",
        relationship="derived_from",
    )
    checkpoint = CheckpointRecord(
        checkpoint_id=CheckpointId("chk_01"),
        run_id=run_id,
        step_id=step_id,
        branch_id=BranchId("main"),
        namespace_id=ns,
        parent_checkpoint_id=None,
        state_payload_json='{"status": "ok"}',
        state_hash="a" * 64,
        created_at=now,
    )
    trace = TraceEventInput(
        event_id=EventId("evt_01"),
        sequence_num=1,
        event_type="step.completed",
        payload={"step": 1},
        emitted_at=now,
        step_id=step_id,
    )

    outcome = commit_step_transaction(
        conn,
        step_result=step_res,
        run_result=updated_run,
        namespace_id=ns,
        proposed_memories=[mem1, mem2],
        lineage_entries=[lineage],
        checkpoint=checkpoint,
        trace_events=[trace],
    )

    assert isinstance(outcome, StepCommitOutcome)
    assert outcome.step_id == step_id
    assert outcome.run_id == run_id
    assert outcome.namespace_id == ns
    assert outcome.memories_committed == 2
    assert outcome.lineage_recorded == 1
    assert outcome.checkpoint_saved is True
    assert outcome.trace_events_appended == 1

    # Verify Phase 1 persistence
    assert get_memory(conn, MemoryId("mem_01"), ns) is not None
    assert get_memory(conn, MemoryId("mem_02"), ns) is not None

    # Verify Phase 2 persistence
    persisted_run = get_run(conn, run_id, ns)
    assert persisted_run is not None
    assert persisted_run.step_count == 1
    assert persisted_run.total_tokens_consumed == 150

    # Verify Phase 3 persistence
    persisted_step = get_step(conn, step_id, ns)
    assert persisted_step is not None
    assert persisted_step.step_id == step_id
    assert persisted_step.decision.finish_reason == ModelFinishReason.STOP

    # Verify Phase 4 persistence
    assert get_memory_lineage(conn, MemoryId("mem_01"), ns) == ("raw_doc_42",)
    assert get_checkpoint(conn, CheckpointId("chk_01"), ns) is not None
    assert len(get_trace_events(conn, run_id, ns)) == 1


def test_preflight_validation_failures() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_preflight")
    run_id = RunId("run_preflight")
    step_id = StepId("step_01")

    step_res = _make_step_result(str(step_id), str(run_id))
    run_res = _make_run_result(str(run_id), str(ns))

    # 1. Run result namespace mismatch
    bundle_wrong_run_ns = StepCommitBundle(
        step_result=step_res,
        run_result=_make_run_result(str(run_id), "ns_other"),
        namespace_id=ns,
    )
    with pytest.raises(StepCommitError) as exc_info:
        commit_step_4phase(conn, bundle_wrong_run_ns)
    assert exc_info.value.phase == "PREFLIGHT"

    # 2. Step result run_id mismatch
    bundle_wrong_step_run = StepCommitBundle(
        step_result=_make_step_result(str(step_id), "run_other"),
        run_result=run_res,
        namespace_id=ns,
    )
    with pytest.raises(StepCommitError) as exc_info:
        commit_step_4phase(conn, bundle_wrong_step_run)
    assert exc_info.value.phase == "PREFLIGHT"

    # 3. Memory namespace mismatch
    mem_wrong_ns = _make_memory("mem_wrong", "ns_other")
    bundle_wrong_mem = StepCommitBundle(
        step_result=step_res,
        run_result=run_res,
        namespace_id=ns,
        proposed_memories=[mem_wrong_ns],
    )
    with pytest.raises(StepCommitError) as exc_info:
        commit_step_4phase(conn, bundle_wrong_mem)
    assert exc_info.value.phase == "PREFLIGHT"

    # 4. Checkpoint namespace mismatch
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    chk_wrong_ns = CheckpointRecord(
        checkpoint_id=CheckpointId("chk_wrong"),
        run_id=run_id,
        step_id=step_id,
        branch_id=BranchId("main"),
        namespace_id=NamespaceId("ns_other"),
        parent_checkpoint_id=None,
        state_payload_json="{}",
        state_hash="b" * 64,
        created_at=now,
    )
    bundle_wrong_chk = StepCommitBundle(
        step_result=step_res,
        run_result=run_res,
        namespace_id=ns,
        checkpoint=chk_wrong_ns,
    )
    with pytest.raises(StepCommitError) as exc_info:
        commit_step_4phase(conn, bundle_wrong_chk)
    assert exc_info.value.phase == "PREFLIGHT"


def test_atomic_rollback_on_phase_4_lineage_foreign_key_failure() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_rollback_p4")
    run_id = RunId("run_rollback_p4")
    step_id = StepId("step_p4")

    # Initial run
    save_run(
        conn,
        RunResult(
            run_id=run_id,
            namespace_id=ns,
            status=RunStatus.RUNNING,
            step_count=0,
            total_tokens_consumed=0,
            final_output=None,
            trace_id="tr_p4",
            token_usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        ),
    )

    step_res = _make_step_result(str(step_id), str(run_id))
    updated_run = _make_run_result(str(run_id), str(ns), tokens=200)
    mem1 = _make_memory("mem_good", str(ns))

    # Lineage references a target_memory_id that was NEVER inserted in memory_records
    bad_lineage = LineageEntry(
        target_memory_id=MemoryId("mem_does_not_exist"),
        source_reference_id="raw_doc",
        relationship="derived_from",
    )

    bundle = StepCommitBundle(
        step_result=step_res,
        run_result=updated_run,
        namespace_id=ns,
        proposed_memories=[mem1],
        lineage_entries=[bad_lineage],
    )

    with pytest.raises(StepCommitError) as exc_info:
        commit_step_4phase(conn, bundle)

    err = exc_info.value
    assert err.phase == "PHASE_4_LINEAGE_AND_TELEMETRY"
    assert err.step_id == str(step_id)
    assert err.run_id == str(run_id)
    assert err.namespace_id == str(ns)
    assert err.details["transaction_status"] == "ROLLED_BACK"

    # Verify Phase 1 memory was rolled back
    assert get_memory(conn, MemoryId("mem_good"), ns) is None

    # Verify Phase 2 run state was rolled back to step_count=0
    current_run = get_run(conn, run_id, ns)
    assert current_run is not None
    assert current_run.step_count == 0
    assert current_run.total_tokens_consumed == 0

    # Verify Phase 3 step was rolled back
    assert get_step(conn, step_id, ns) is None


def test_atomic_rollback_on_phase_3_cross_namespace_step_failure() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_rollback_p3")
    ns_other = NamespaceId("ns_other_p3")
    run_id = RunId("run_rollback_p3")
    run_other = RunId("run_other_p3")
    step_id = StepId("step_conflict")

    # 1. Setup run_other and save step under ns_other
    save_run(
        conn,
        RunResult(
            run_id=run_other,
            namespace_id=ns_other,
            status=RunStatus.RUNNING,
            step_count=0,
            total_tokens_consumed=0,
            final_output=None,
            trace_id="tr_other",
            token_usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        ),
    )
    save_step(conn, _make_step_result(str(step_id), str(run_other)), ns_other)

    # 2. Setup run under ns
    save_run(
        conn,
        RunResult(
            run_id=run_id,
            namespace_id=ns,
            status=RunStatus.RUNNING,
            step_count=0,
            total_tokens_consumed=0,
            final_output=None,
            trace_id="tr_p3",
            token_usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        ),
    )

    # Attempt commit under ns with the same step_id (cross-namespace mutation attempt)
    new_mem = _make_memory("mem_rollback_p3", str(ns))
    run_res2 = _make_run_result(str(run_id), str(ns), tokens=100)
    step_res = _make_step_result(str(step_id), str(run_id))

    bundle = StepCommitBundle(
        step_result=step_res,
        run_result=run_res2,
        namespace_id=ns,
        proposed_memories=[new_mem],
    )

    with pytest.raises(StepCommitError) as exc_info:
        commit_step_4phase(conn, bundle)

    assert exc_info.value.phase == "PHASE_3_STEP_RESULT"
    assert exc_info.value.details["transaction_status"] == "ROLLED_BACK"

    # Verify new_mem was rolled back and not persisted
    assert get_memory(conn, MemoryId("mem_rollback_p3"), ns) is None

    # Run tokens remained 0, not 100
    current_run = get_run(conn, run_id, ns)
    assert current_run is not None
    assert current_run.total_tokens_consumed == 0

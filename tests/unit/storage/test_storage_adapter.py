"""Unit tests for the unified SQLiteStorageAdapter conforming to StoragePort."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from memory_aware_agent_harness.contracts import (
    BranchId,
    CheckpointId,
    EventId,
    MemoryFilter,
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    MemoryStoreProtocol,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepId,
    StepResult,
    StorageError,
    StoragePort,
    StorageVersionConflictError,
    ToolCallStatus,
    ToolInvocationId,
    ToolResult,
)
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter
from memory_aware_agent_harness.storage.checkpoints_repo import (
    DEFAULT_BRANCH_ID,
    CheckpointRecord,
)
from memory_aware_agent_harness.storage.connection import (
    StorageSettings,
    create_connection,
)
from memory_aware_agent_harness.storage.step_commit import LineageEntry, TraceEventInput


def _sample_memory(
    memory_id: str,
    namespace: str,
    text: str = "Test memory content",
    kind: MemoryKind = MemoryKind.SEMANTIC,
) -> MemoryRecord:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    return MemoryRecord(
        id=MemoryId(memory_id),
        namespace_id=NamespaceId(namespace),
        content=text,
        kind=kind,
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        origin=MemoryOrigin.AGENT_OBSERVATION,
        created_at=now,
        updated_at=now,
    )


def _sample_run(run_id: str, namespace: str) -> RunResult:
    return RunResult(
        run_id=RunId(run_id),
        namespace_id=NamespaceId(namespace),
        status=RunStatus.RUNNING,
        step_count=0,
        total_tokens_consumed=0,
        final_output=None,
        trace_id=f"tr_{run_id}",
    )


def _sample_step(step_id: str, run_id: str, step_num: int = 1) -> StepResult:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    return StepResult(
        step_id=StepId(step_id),
        run_id=RunId(run_id),
        step_number=step_num,
        decision=ModelDecision(
            decision_id=f"dec_{step_id}",
            step_id=StepId(step_id),
            finish_reason=ModelFinishReason.STOP,
            content="Done step",
        ),
        tool_results=(),
        memories_proposed=0,
        memories_committed=0,
        tokens_consumed=150,
        completed_at=now,
    )


def test_protocol_conformance() -> None:
    """Verify SQLiteStorageAdapter conforms to StoragePort and MemoryStoreProtocol."""
    adapter = SQLiteStorageAdapter(":memory:")
    assert isinstance(adapter, StoragePort)
    assert isinstance(adapter, MemoryStoreProtocol)
    adapter.close()


def test_lifecycle_and_context_manager(tmp_path: Path) -> None:
    """Verify connection creation, settings, closing, and context management."""
    db_file = tmp_path / "test.db"
    settings = StorageSettings(
        database_path=str(db_file),
        timeout_seconds=3.0,
        synchronous="NORMAL",
    )

    with SQLiteStorageAdapter(settings=settings) as adapter:
        assert not bool(adapter._closed)
        assert adapter.conn is not None

        # Verify tables exist via auto_migrate
        row = adapter.conn.execute(
            "SELECT count(*) as cnt FROM sqlite_master "
            "WHERE type='table' AND name='memory_records';"
        ).fetchone()
        assert row is not None and row["cnt"] == 1

    # Should be closed after context exit
    assert bool(adapter._closed) is True
    with pytest.raises(StorageError, match="is closed"):
        _ = adapter.conn
    with pytest.raises(StorageError, match="is closed"):
        adapter.get(MemoryId("m1"), NamespaceId("ns"))


def test_unowned_connection_lifecycle() -> None:
    """Verify adapter using external connection does not close it on close()."""
    conn = create_connection(":memory:")
    adapter = SQLiteStorageAdapter(connection=conn, auto_migrate=True)

    adapter.put(_sample_memory("m1", "ns1"))
    assert adapter.get(MemoryId("m1"), NamespaceId("ns1")) is not None

    adapter.close()
    assert bool(adapter._closed) is True

    # External connection remains active and usable
    row = conn.execute("SELECT memory_id FROM memory_records WHERE memory_id = 'm1';").fetchone()
    assert row is not None
    conn.close()


def test_memory_crud_and_search() -> None:
    """Verify memory put, get, search, list_active, tombstone, and optimistic concurrency."""
    ns = NamespaceId("ns_mem")
    adapter = SQLiteStorageAdapter(":memory:")

    m1 = _sample_memory(
        "m1", "ns_mem", "Deployment instruction for production", MemoryKind.SEMANTIC
    )
    m2 = _sample_memory("m2", "ns_mem", "User prefers concise answers", MemoryKind.EPISODIC)
    m3 = _sample_memory("m3", "ns_mem", "Draft scratchpad details", MemoryKind.WORKING)

    adapter.put(m1)
    adapter.put(m2)
    adapter.put(m3)

    # get
    fetched = adapter.get(MemoryId("m1"), ns)
    assert fetched is not None
    assert fetched.content == "Deployment instruction for production"
    assert fetched.version == 1

    # search
    search_res = adapter.search(ns, "deployment", [MemoryKind.SEMANTIC], limit=10)
    assert len(search_res) == 1
    assert search_res[0].id == MemoryId("m1")

    # search_scored
    query_filter = MemoryFilter(
        namespace_id=ns,
        query_text="Deployment instruction",
        kinds=(MemoryKind.SEMANTIC, MemoryKind.EPISODIC),
        limit=5,
    )
    scored = adapter.search_scored(query_filter)
    assert len(scored) >= 1
    assert scored[0].record.id == MemoryId("m1")
    assert scored[0].score > 0.0

    # list_active
    active_semantics = adapter.list_active(ns, MemoryKind.SEMANTIC)
    assert len(active_semantics) == 1
    assert active_semantics[0].id == MemoryId("m1")

    # optimistic update
    updated_m1 = MemoryRecord(
        id=m1.id,
        namespace_id=m1.namespace_id,
        content="Updated deployment instruction",
        kind=m1.kind,
        state=m1.state,
        confidence=m1.confidence,
        salience=m1.salience,
        created_at=m1.created_at,
        updated_at=m1.updated_at,
        origin=m1.origin,
        version=2,
    )
    adapter.update_optimistic(updated_m1, expected_version=1)

    fetched_updated = adapter.get(MemoryId("m1"), ns)
    assert fetched_updated is not None
    assert fetched_updated.content == "Updated deployment instruction"
    assert fetched_updated.version == 2

    # optimistic conflict
    with pytest.raises(StorageVersionConflictError):
        adapter.update_optimistic(updated_m1, expected_version=1)

    # tombstone
    adapter.tombstone(MemoryId("m1"), ns, reason="superseded by m4")
    assert adapter.get(MemoryId("m1"), ns) is None
    assert len(adapter.list_active(ns, MemoryKind.SEMANTIC)) == 0

    # lineage
    adapter.record_memory_lineage(MemoryId("m2"), ns, "source_pref_doc", "derived_from")
    lineage = adapter.get_memory_lineage(MemoryId("m2"), ns)
    assert lineage == ("source_pref_doc",)

    adapter.close()


def test_runs_and_steps_persistence() -> None:
    """Verify run and step persistence and retrieval."""
    ns = NamespaceId("ns_runs")
    run_id = RunId("run_100")
    step_id = StepId("step_100")

    adapter = SQLiteStorageAdapter(":memory:")
    run = _sample_run(str(run_id), str(ns))
    adapter.save_run(run)

    fetched_run = adapter.get_run(run_id, ns)
    assert fetched_run is not None
    assert fetched_run.run_id == run_id
    assert fetched_run.status == RunStatus.RUNNING

    step = _sample_step(str(step_id), str(run_id), step_num=1)
    adapter.save_step(step, ns)

    fetched_step = adapter.get_step(step_id, ns)
    assert fetched_step is not None
    assert fetched_step.step_id == step_id
    assert fetched_step.decision.finish_reason == ModelFinishReason.STOP

    adapter.close()


def test_tool_invocations_lifecycle() -> None:
    """Verify recording tool intent, result, and retrieving ToolResult."""
    ns = NamespaceId("ns_tools")
    run_id = RunId("run_tool")
    step_id = StepId("step_tool")
    inv_id = ToolInvocationId("inv_01")
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    comp = datetime(2026, 10, 14, 12, 0, 1, tzinfo=UTC)

    adapter = SQLiteStorageAdapter(":memory:")
    adapter.save_run(_sample_run(str(run_id), str(ns)))
    adapter.save_step(_sample_step(str(step_id), str(run_id)), ns)

    # Record intent without explicit run_id (should resolve run_id from run_steps)
    adapter.record_tool_intent(
        invocation_id=inv_id,
        step_id=step_id,
        namespace_id=ns,
        tool_name="calculator",
        arguments={"expression": "2 + 2"},
        side_effect=SideEffectType.READ_ONLY,
        idempotency_key="idemp_calc_01",
        dispatched_at=now,
    )

    # Find by idempotency key
    rec = adapter.find_invocation_by_idempotency_key("idemp_calc_01", ns)
    assert rec is not None
    assert rec.invocation_id == inv_id
    assert rec.status == ToolCallStatus.PENDING

    # Record result
    res = ToolResult(
        invocation_id=inv_id,
        tool_name="calculator",
        status=ToolCallStatus.COMPLETED,
        side_effect=SideEffectType.READ_ONLY,
        idempotency_key="idemp_calc_01",
        result={"answer": 4},
    )
    adapter.record_tool_result(res, ns, completed_at=comp)

    # Retrieve through StoragePort
    retrieved_res = adapter.get_tool_invocation(inv_id, ns)
    assert retrieved_res is not None
    assert retrieved_res.status == ToolCallStatus.COMPLETED
    assert retrieved_res.result == {"answer": 4}
    assert retrieved_res.execution_time_ms == 1000.0

    # Retrieve raw record
    raw_rec = adapter.get_tool_invocation_record(inv_id, ns)
    assert raw_rec is not None
    assert raw_rec.completed_at == comp

    adapter.close()


def test_tool_intent_missing_step_raises_record_not_found() -> None:
    """Verify record_tool_intent without run_id raises RecordNotFoundError.

    Must fail closed if step does not exist.
    """
    ns = NamespaceId("ns_missing")
    adapter = SQLiteStorageAdapter(":memory:")

    with pytest.raises(RecordNotFoundError, match="Parent step"):
        adapter.record_tool_intent(
            invocation_id=ToolInvocationId("inv_x"),
            step_id=StepId("step_missing"),
            namespace_id=ns,
            tool_name="tool_x",
            arguments={},
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key="key_x",
            dispatched_at=datetime.now(UTC),
        )
    adapter.close()


def test_checkpoints_storage() -> None:
    """Verify save_checkpoint, get_checkpoint, get_checkpoint_record, get_latest_checkpoint."""
    ns = NamespaceId("ns_chk")
    run_id = RunId("run_chk")
    step_id = StepId("step_chk")
    chk_id = CheckpointId("chk_01")
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    adapter = SQLiteStorageAdapter(":memory:")
    adapter.save_run(_sample_run(str(run_id), str(ns)))
    adapter.save_step(_sample_step(str(step_id), str(run_id)), ns)

    adapter.save_checkpoint(
        checkpoint_id=chk_id,
        run_id=run_id,
        step_id=step_id,
        branch_id=DEFAULT_BRANCH_ID,
        namespace_id=ns,
        state_payload={"state_var": "val_123"},
        created_at=now,
    )

    # get_checkpoint (returns mapping)
    payload = adapter.get_checkpoint(chk_id, ns)
    assert payload == {"state_var": "val_123"}

    # get_checkpoint_record
    record = adapter.get_checkpoint_record(chk_id, ns)
    assert record is not None
    assert record.checkpoint_id == chk_id
    assert len(record.state_hash) == 64

    # get_latest_checkpoint
    latest = adapter.get_latest_checkpoint(run_id, ns)
    assert latest is not None
    assert latest.checkpoint_id == chk_id

    # Non-existent
    assert adapter.get_checkpoint(CheckpointId("chk_none"), ns) is None
    adapter.close()


def test_trace_events_storage() -> None:
    """Verify append_trace_event, get_trace_event_records, and get_trace_events."""
    ns = NamespaceId("ns_trace")
    run_id = RunId("run_tr")
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    adapter = SQLiteStorageAdapter(":memory:")
    adapter.save_run(_sample_run(str(run_id), str(ns)))

    adapter.append_trace_event(
        event_id=EventId("ev_01"),
        run_id=run_id,
        namespace_id=ns,
        sequence_num=1,
        event_type="test.event",
        payload={"key": "val"},
        emitted_at=now,
    )

    # get_trace_event_records
    records = adapter.get_trace_event_records(run_id, ns)
    assert len(records) == 1
    assert records[0].event_id == EventId("ev_01")
    assert records[0].payload() == {"key": "val"}

    # get_trace_events (mappings)
    mappings = adapter.get_trace_events(run_id, ns)
    assert len(mappings) == 1
    assert mappings[0]["event_id"] == "ev_01"
    assert mappings[0]["event_type"] == "test.event"
    assert mappings[0]["payload"] == {"key": "val"}

    adapter.close()


def test_commit_step_transaction_through_adapter() -> None:
    """Verify commit_step and commit_step_transaction methods on adapter."""
    ns = NamespaceId("ns_step_commit")
    run_id = RunId("run_sc")
    step_id = StepId("step_sc")
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    adapter = SQLiteStorageAdapter(":memory:")
    initial_run = _sample_run(str(run_id), str(ns))
    adapter.save_run(initial_run)

    step_res = _sample_step(str(step_id), str(run_id))
    updated_run = RunResult(
        run_id=run_id,
        namespace_id=ns,
        status=RunStatus.RUNNING,
        step_count=1,
        total_tokens_consumed=50,
        final_output=None,
        trace_id="tr_sc",
    )
    mem = _sample_memory("mem_sc", str(ns))
    chk = CheckpointRecord(
        checkpoint_id=CheckpointId("chk_sc"),
        run_id=run_id,
        step_id=step_id,
        branch_id=BranchId("main"),
        namespace_id=ns,
        parent_checkpoint_id=None,
        state_payload_json='{"status": "ok"}',
        state_hash="c" * 64,
        created_at=now,
    )
    tr = TraceEventInput(
        event_id=EventId("ev_sc"),
        sequence_num=1,
        event_type="step.done",
        payload={"done": True},
        emitted_at=now,
        step_id=step_id,
    )
    lineage = LineageEntry(
        target_memory_id=MemoryId("mem_sc"),
        source_reference_id="doc_ref_1",
    )

    outcome = adapter.commit_step_transaction(
        step_result=step_res,
        run_result=updated_run,
        namespace_id=ns,
        proposed_memories=[mem],
        lineage_entries=[lineage],
        checkpoint=chk,
        trace_events=[tr],
    )

    assert outcome.step_id == step_id
    assert outcome.memories_committed == 1
    assert outcome.checkpoint_saved is True
    assert outcome.trace_events_appended == 1

    # Check persistence
    assert adapter.get(MemoryId("mem_sc"), ns) is not None
    assert adapter.get_run(run_id, ns) is not None
    assert adapter.get_step(step_id, ns) is not None
    assert adapter.get_checkpoint(CheckpointId("chk_sc"), ns) is not None

    adapter.close()


def test_commit_step_bundle_direct_and_transaction() -> None:
    """Verify commit_step with StepCommitBundle and adapter.transaction()."""
    from memory_aware_agent_harness.storage.step_commit import StepCommitBundle

    ns = NamespaceId("ns_bundle")
    run_id = RunId("run_bundle")
    step_id = StepId("step_bundle")

    with SQLiteStorageAdapter(":memory:") as adapter:
        # Test transaction context
        with adapter.transaction():
            adapter.save_run(_sample_run(str(run_id), str(ns)))

        assert adapter.get_run(run_id, ns) is not None

        # Test commit_step with StepCommitBundle
        bundle = StepCommitBundle(
            step_result=_sample_step(str(step_id), str(run_id)),
            run_result=RunResult(
                run_id=run_id,
                namespace_id=ns,
                status=RunStatus.RUNNING,
                step_count=1,
                total_tokens_consumed=10,
                final_output=None,
                trace_id="tr_b",
            ),
            namespace_id=ns,
        )
        outcome = adapter.commit_step(bundle)
        assert outcome.step_id == step_id
        assert outcome.run_id == run_id

        # Missing tool invocation returns None
        assert adapter.get_tool_invocation(ToolInvocationId("nonexistent"), ns) is None
        assert adapter.get_tool_invocation_record(ToolInvocationId("nonexistent"), ns) is None


def test_record_step_start() -> None:
    """Verify record_step_start sets step state to running."""
    ns = NamespaceId("ns_step_start")
    run_id = RunId("run_step_start")
    step_id = StepId("step_step_start")

    with SQLiteStorageAdapter(":memory:") as adapter:
        adapter.save_run(_sample_run(str(run_id), str(ns)))
        adapter.record_step_start(
            step_id=step_id,
            run_id=run_id,
            namespace_id=ns,
            step_number=1,
            started_at=datetime.now(UTC),
        )
        row = adapter._conn.execute(
            "SELECT state, step_number FROM run_steps WHERE step_id = ?;",
            (str(step_id),),
        ).fetchone()
        assert row is not None
        assert row["state"] == "running"
        assert row["step_number"] == 1

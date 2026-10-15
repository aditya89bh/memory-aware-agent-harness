"""Unit tests for checkpoint anti-resurrection safeguard on state restore (Task 22).

Enforces SPEC.md Section 6.5, ARCHITECTURE.md Section 6.3, and ADR-0008:
- Prior to rehydrating state, query active tombstones in namespace.
- Strip and purge all tombstoned memory IDs from restored working memory and candidate pool.
- Refuse restoration with StateResurrectionConflictError if critical memories are purged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    CheckpointId,
    CrossNamespaceAccessError,
    MemoryId,
    MemoryKind,
    MemoryRecord,
    MemoryState,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    ReplayDivergenceError,
    RunId,
    RunResult,
    RunStatus,
    StateResurrectionConflictError,
    StepId,
    compute_state_hash,
)
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.rehydration import CheckpointRehydrationEngine
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter
from memory_aware_agent_harness.storage.checkpoints_repo import (
    DEFAULT_BRANCH_ID,
    CheckpointRecord,
)


def _create_sample_record(
    mem_id: str,
    namespace_id: NamespaceId,
    content: str = "Test memory",
) -> MemoryRecord:
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    return MemoryRecord(
        id=MemoryId(mem_id),
        namespace_id=namespace_id,
        kind=MemoryKind.WORKING,
        content=content,
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
    )


def _sample_run(run_id: RunId, ns: NamespaceId) -> RunResult:
    return RunResult(
        run_id=run_id,
        namespace_id=ns,
        status=RunStatus.RUNNING,
        step_count=0,
        total_tokens_consumed=0,
        final_output=None,
        trace_id=f"tr_{run_id}",
    )


def test_anti_resurrection_purges_tombstoned_memory_ids() -> None:
    """Tombstone reconciliation purges deleted memory IDs from restored checkpoint payload."""
    storage = SQLiteStorageAdapter(":memory:")
    try:
        ns = NamespaceId("ns_safeguard")
        run_id = RunId("run_1")
        step_id = StepId("step_1")
        storage.save_run(_sample_run(run_id, ns))
        storage.record_step_start(
            step_id,
            run_id,
            ns,
            1,
            datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        )

        rec1 = _create_sample_record("mem_active", ns, "Active preference")
        rec2 = _create_sample_record("mem_erased", ns, "Forgotten fact")
        storage.put(rec1)
        storage.put(rec2)

        # Tombstone rec2
        storage.tombstone(rec2.id, ns, "GDPR erasure requested")

        # Create historical checkpoint payload containing both memories
        raw_payload = {
            "run_id": str(run_id),
            "step_id": str(step_id),
            "step_number": 1,
            "working_memory_ids": ["mem_active", "mem_erased"],
            "working_memory": [
                {"id": "mem_active", "content": "Active preference"},
                {"id": "mem_erased", "content": "Forgotten fact"},
            ],
            "candidate_memory_ids": ["mem_erased"],
        }
        ckpt = CheckpointRecord(
            checkpoint_id=CheckpointId("ckpt_hist"),
            run_id=run_id,
            step_id=step_id,
            branch_id=DEFAULT_BRANCH_ID,
            namespace_id=ns,
            parent_checkpoint_id=None,
            state_payload_json=json.dumps(raw_payload, sort_keys=True),
            state_hash=compute_state_hash(raw_payload),
            created_at=datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        )

        engine = CheckpointRehydrationEngine(storage)
        rehydrated = engine.rehydrate(ckpt, ns)

        # Assert erased memory was purged
        assert MemoryId("mem_erased") in rehydrated.purged_memory_ids
        assert MemoryId("mem_active") not in rehydrated.purged_memory_ids
        assert rehydrated.active_tombstones_count >= 1

        # Assert sanitized payload excludes erased memory
        restored_ids = rehydrated.state_payload["working_memory_ids"]
        assert restored_ids == ["mem_active"]

        restored_objs = rehydrated.state_payload["working_memory"]
        assert len(restored_objs) == 1
        assert restored_objs[0]["id"] == "mem_active"

        # Candidate memory pool also purged
        assert rehydrated.state_payload["candidate_memory_ids"] == []
    finally:
        storage.close()


def test_anti_resurrection_conflict_refusal_on_critical_memory() -> None:
    """StateResurrectionConflictError is raised if a critical memory is tombstoned."""
    storage = SQLiteStorageAdapter(":memory:")
    try:
        ns = NamespaceId("ns_conflict")
        run_id = RunId("run_1")
        step_id = StepId("step_1")
        storage.save_run(_sample_run(run_id, ns))
        storage.record_step_start(
            step_id,
            run_id,
            ns,
            1,
            datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        )

        rec = _create_sample_record("mem_critical", ns, "Essential authentication token")
        storage.put(rec)
        storage.tombstone(rec.id, ns, "Compromised credential revocation")

        raw_payload = {
            "run_id": str(run_id),
            "step_id": str(step_id),
            "working_memory_ids": ["mem_critical"],
        }
        ckpt = CheckpointRecord(
            checkpoint_id=CheckpointId("ckpt_crit"),
            run_id=run_id,
            step_id=step_id,
            branch_id=DEFAULT_BRANCH_ID,
            namespace_id=ns,
            parent_checkpoint_id=None,
            state_payload_json=json.dumps(raw_payload, sort_keys=True),
            state_hash=compute_state_hash(raw_payload),
            created_at=datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        )

        engine = CheckpointRehydrationEngine(storage)

        # Critical memory tombstoned -> raises StateResurrectionConflictError
        with pytest.raises(StateResurrectionConflictError) as exc_info:
            engine.rehydrate(
                ckpt,
                ns,
                critical_memory_ids=[MemoryId("mem_critical")],
            )

        err = exc_info.value
        assert err.error_code == "STATE_RESURRECTION_CONFLICT"
        assert MemoryId("mem_critical") in err.conflicting_memory_ids
        assert "mem_critical" in err.message
    finally:
        storage.close()


def test_anti_resurrection_conflict_strategy_refuse_all() -> None:
    """Strategy 'refuse' rejects restoration if ANY memory in checkpoint is tombstoned."""
    storage = SQLiteStorageAdapter(":memory:")
    try:
        ns = NamespaceId("ns_refuse")
        run_id = RunId("run_1")
        step_id = StepId("step_1")
        storage.save_run(_sample_run(run_id, ns))
        storage.record_step_start(
            step_id,
            run_id,
            ns,
            1,
            datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        )

        rec = _create_sample_record("mem_any", ns, "Casual observation")
        storage.put(rec)
        storage.tombstone(rec.id, ns, "User requested forget")

        raw_payload = {
            "run_id": str(run_id),
            "step_id": str(step_id),
            "working_memory_ids": ["mem_any"],
        }
        ckpt = CheckpointRecord(
            checkpoint_id=CheckpointId("ckpt_refuse"),
            run_id=run_id,
            step_id=step_id,
            branch_id=DEFAULT_BRANCH_ID,
            namespace_id=ns,
            parent_checkpoint_id=None,
            state_payload_json=json.dumps(raw_payload, sort_keys=True),
            state_hash=compute_state_hash(raw_payload),
            created_at=datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        )

        engine = CheckpointRehydrationEngine(storage)
        with pytest.raises(StateResurrectionConflictError) as exc_info:
            engine.rehydrate(ckpt, ns, conflict_strategy="refuse")

        assert exc_info.value.error_code == "STATE_RESURRECTION_CONFLICT"
        assert MemoryId("mem_any") in exc_info.value.conflicting_memory_ids
    finally:
        storage.close()


def test_anti_resurrection_cross_namespace_rejected() -> None:
    """Cross-namespace checkpoint restore attempt is rejected with CrossNamespaceAccessError."""
    storage = SQLiteStorageAdapter(":memory:")
    try:
        ns_a = NamespaceId("ns_tenant_a")
        ns_b = NamespaceId("ns_tenant_b")

        raw_payload = {"run_id": "run_a", "step_id": "step_1"}
        ckpt = CheckpointRecord(
            checkpoint_id=CheckpointId("ckpt_a"),
            run_id=RunId("run_a"),
            step_id=StepId("step_1"),
            branch_id=DEFAULT_BRANCH_ID,
            namespace_id=ns_a,
            parent_checkpoint_id=None,
            state_payload_json=json.dumps(raw_payload, sort_keys=True),
            state_hash=compute_state_hash(raw_payload),
            created_at=datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        )

        engine = CheckpointRehydrationEngine(storage)
        with pytest.raises(CrossNamespaceAccessError):
            engine.rehydrate(ckpt, ns_b)
    finally:
        storage.close()


def test_anti_resurrection_tampered_state_divergence_rejected() -> None:
    """Tampered checkpoint payload fails integrity check with ReplayDivergenceError."""
    storage = SQLiteStorageAdapter(":memory:")
    try:
        ns = NamespaceId("ns_tamper")
        raw_payload = {"run_id": "run_1", "step_id": "step_1", "tokens": 100}
        valid_hash = compute_state_hash(raw_payload)

        # Alter payload without updating state_hash
        tampered_payload = {"run_id": "run_1", "step_id": "step_1", "tokens": 999999}
        ckpt = CheckpointRecord(
            checkpoint_id=CheckpointId("ckpt_tampered"),
            run_id=RunId("run_1"),
            step_id=StepId("step_1"),
            branch_id=DEFAULT_BRANCH_ID,
            namespace_id=ns,
            parent_checkpoint_id=None,
            state_payload_json=json.dumps(tampered_payload, sort_keys=True),
            state_hash=valid_hash,
            created_at=datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        )

        engine = CheckpointRehydrationEngine(storage)
        with pytest.raises(ReplayDivergenceError):
            engine.rehydrate(ckpt, ns, verify_integrity=True)
    finally:
        storage.close()


def test_agent_runtime_restore_from_checkpoint_anti_resurrection() -> None:
    """AgentRuntime restores state from checkpoint purging tombstoned memories."""
    storage = SQLiteStorageAdapter(":memory:")
    try:
        ns = NamespaceId("ns_runtime_restore")

        # Step 1: Save run and step in storage
        run_id = RunId("run_rt")
        step_id = StepId("step_rt")
        storage.save_run(_sample_run(run_id, ns))
        storage.record_step_start(
            step_id,
            run_id,
            ns,
            1,
            datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        )

        # Save memories
        rec1 = _create_sample_record("mem_rt_keep", ns, "Keep this")
        rec2 = _create_sample_record("mem_rt_purge", ns, "Purge this")
        storage.put(rec1)
        storage.put(rec2)

        # Save checkpoint with working memory ids in payload
        ckpt_id = CheckpointId("ckpt_rt_1")
        payload = {
            "run_id": str(run_id),
            "step_id": str(step_id),
            "step_number": 3,
            "working_memory_ids": ["mem_rt_keep", "mem_rt_purge"],
        }
        storage.save_checkpoint(
            checkpoint_id=ckpt_id,
            run_id=run_id,
            step_id=step_id,
            branch_id=DEFAULT_BRANCH_ID,
            namespace_id=ns,
            state_payload=payload,
            created_at=datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        )

        # Now tombstone mem_rt_purge
        storage.tombstone(rec2.id, ns, "Deleted per privacy request")

        def mock_handler(*args: Any, **kwargs: Any) -> ModelDecision:
            return ModelDecision(
                decision_id="dec_1",
                step_id=step_id,
                finish_reason=ModelFinishReason.STOP,
                content="Restored",
                tokens_used=5,
            )

        model = FakeModelAdapter(default_handler=mock_handler)
        runtime = AgentRuntime(model=model, storage=storage)

        # Restore from checkpoint
        state, rehydrated = runtime.restore_from_checkpoint(ckpt_id, ns)

        # Assert state metadata and counter restored safely
        assert state.step_count == 3
        assert state.metadata["rehydrated_from_checkpoint"] == "ckpt_rt_1"
        assert state.metadata["working_memory_ids"] == ["mem_rt_keep"]
        assert MemoryId("mem_rt_purge") in rehydrated.purged_memory_ids
        assert "mem_rt_purge" in state.metadata["purged_memory_ids"]
    finally:
        storage.close()

"""Tests for contract-level validation rules across public dataclasses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    SCHEMA_VERSION,
    BranchId,
    CandidateStatus,
    CheckpointId,
    ContextManifest,
    CrossNamespaceAccessError,
    EventId,
    FrozenDict,
    MemoryCandidate,
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
    ReplayDivergenceError,
    RunBudgets,
    RunId,
    RunResult,
    RunStatus,
    ScoredMemoryItem,
    SideEffectType,
    StepId,
    StepResult,
    StorageError,
    StoragePort,
    StorageVersionConflictError,
    TokenUsage,
    TombstoneActiveError,
    ToolCallStatus,
    ToolInvocationId,
    ToolResult,
    compute_state_hash,
    verify_state_hash,
)
from memory_aware_agent_harness.validation import (
    BoundaryValidationError,
    validate_memory_filter,
    validate_scored_memory_item,
)


def test_memory_record_requires_tz_aware_datetimes() -> None:
    """Naive datetimes without tzinfo must be rejected."""
    naive_dt = datetime(2026, 10, 11, 12, 0, 0)  # No tzinfo

    with pytest.raises(ValueError, match=r"created_at must be timezone-aware"):
        MemoryRecord(
            id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.SEMANTIC,
            content="Valid fact",
            state=MemoryState.ACTIVE,
            confidence=0.9,
            salience=0.5,
            created_at=naive_dt,
            updated_at=datetime.now(UTC),
        )

    with pytest.raises(ValueError, match=r"updated_at must be timezone-aware"):
        MemoryRecord(
            id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.SEMANTIC,
            content="Valid fact",
            state=MemoryState.ACTIVE,
            confidence=0.9,
            salience=0.5,
            created_at=datetime.now(UTC),
            updated_at=naive_dt,
        )

    with pytest.raises(ValueError, match=r"expires_at must be timezone-aware"):
        MemoryRecord(
            id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.SEMANTIC,
            content="Valid fact",
            state=MemoryState.ACTIVE,
            confidence=0.9,
            salience=0.5,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            expires_at=naive_dt,
        )


def test_memory_record_confidence_and_salience_bounds() -> None:
    """Confidence and salience must be within [0.0, 1.0]."""
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match=r"confidence must be between 0\.0 and 1\.0"):
        MemoryRecord(
            id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.SEMANTIC,
            content="Fact",
            state=MemoryState.ACTIVE,
            confidence=1.05,
            salience=0.5,
            created_at=now,
            updated_at=now,
        )

    with pytest.raises(ValueError, match=r"salience must be between 0\.0 and 1\.0"):
        MemoryRecord(
            id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.SEMANTIC,
            content="Fact",
            state=MemoryState.ACTIVE,
            confidence=0.8,
            salience=-0.1,
            created_at=now,
            updated_at=now,
        )


def test_memory_record_empty_fields_rejected() -> None:
    """Empty IDs or empty content must raise ValueError."""
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match=r"MemoryRecord\.id must not be empty"):
        MemoryRecord(
            id=MemoryId("   "),
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.SEMANTIC,
            content="Fact",
            state=MemoryState.ACTIVE,
            confidence=0.8,
            salience=0.5,
            created_at=now,
            updated_at=now,
        )

    with pytest.raises(ValueError, match=r"MemoryRecord\.content must not be empty"):
        MemoryRecord(
            id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.SEMANTIC,
            content="",
            state=MemoryState.ACTIVE,
            confidence=0.8,
            salience=0.5,
            created_at=now,
            updated_at=now,
        )


def test_memory_candidate_validation() -> None:
    """Candidate validation status defaults to PENDING and validates salience."""
    cand = MemoryCandidate(
        candidate_id="cand_01",
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.EPISODIC,
        content="Observation from step",
        proposed_salience=0.7,
        source_step_id=StepId("step_01"),
        rationale="Important interaction",
    )
    assert cand.validation_status == CandidateStatus.PENDING

    with pytest.raises(ValueError, match=r"proposed_salience must be between 0\.0 and 1\.0"):
        MemoryCandidate(
            candidate_id="cand_01",
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.EPISODIC,
            content="Observation",
            proposed_salience=1.5,
            source_step_id=StepId("step_01"),
            rationale="Rationale",
        )


def test_tool_result_validation() -> None:
    """Negative execution time or empty IDs raise ValueError."""
    with pytest.raises(ValueError, match=r"execution_time_ms must be >= 0\.0"):
        ToolResult(
            invocation_id=ToolInvocationId("inv_01"),
            tool_name="test_tool",
            status=ToolCallStatus.COMPLETED,
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key="key_1",
            execution_time_ms=-5.0,
        )


def test_context_manifest_validation() -> None:
    """Manifest tokens and budgets must be non-negative."""
    with pytest.raises(ValueError, match=r"budget_limit must be >= 0"):
        ContextManifest(
            manifest_id="man_01",
            step_id=StepId("step_01"),
            included_items=(),
            excluded_item_ids=FrozenDict({}),
            total_tokens=100,
            budget_limit=-1,
            compiled_prompt="",
        )


def test_step_and_run_result_validation() -> None:
    """StepResult and RunResult non-negative counters enforced."""
    now = datetime.now(UTC)
    dec = ModelDecision(
        decision_id="dec_01",
        step_id=StepId("step_01"),
        finish_reason=ModelFinishReason.STOP,
        tokens_used=10,
    )

    with pytest.raises(ValueError, match=r"step_number must be >= 0"):
        StepResult(
            step_id=StepId("step_01"),
            run_id=RunId("run_01"),
            step_number=-1,
            decision=dec,
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
            tokens_consumed=10,
            completed_at=now,
        )

    with pytest.raises(ValueError, match=r"RunResult\.step_count must be >= 0"):
        RunResult(
            run_id=RunId("run_01"),
            namespace_id=NamespaceId("ns_01"),
            status=RunStatus.COMPLETED,
            step_count=-1,
            total_tokens_consumed=10,
            final_output="Done",
            trace_id="tr_01",
        )


def test_m03_strict_enum_coercion_and_rejection() -> None:
    """M-03 regression test: Enums are strictly coerced from valid strings and invalid rejected."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)

    # Valid string for kind & state coerces to actual Enum instance
    rec = MemoryRecord(
        id=MemoryId("mem_coerce"),
        namespace_id=NamespaceId("ns_01"),
        kind="semantic",  # type: ignore[arg-type]
        content="Testing enum coercion",
        state="active",  # type: ignore[arg-type]
        confidence=0.9,
        salience=0.5,
        created_at=now,
        updated_at=now,
    )
    assert rec.kind is MemoryKind.SEMANTIC
    assert rec.state is MemoryState.ACTIVE

    # Invalid enum string is rejected with ValueError
    with pytest.raises(ValueError, match=r"MemoryRecord\.kind must be a valid MemoryKind"):
        MemoryRecord(
            id=MemoryId("mem_invalid_kind"),
            namespace_id=NamespaceId("ns_01"),
            kind="bogus_kind",  # type: ignore[arg-type]
            content="Invalid",
            state=MemoryState.ACTIVE,
            confidence=0.9,
            salience=0.5,
            created_at=now,
            updated_at=now,
        )

    # Non-str, non-enum type is rejected with TypeError
    with pytest.raises(
        TypeError, match=r"MemoryRecord\.kind must be an instance of MemoryKind or str"
    ):
        MemoryRecord(
            id=MemoryId("mem_bad_type"),
            namespace_id=NamespaceId("ns_01"),
            kind=123,  # type: ignore[arg-type]
            content="Invalid",
            state=MemoryState.ACTIVE,
            confidence=0.9,
            salience=0.5,
            created_at=now,
            updated_at=now,
        )

    # ToolResult enum coercion & rejection
    tr = ToolResult(
        invocation_id=ToolInvocationId("inv_01"),
        tool_name="test_tool",
        status="completed",  # type: ignore[arg-type]
        side_effect="read_only",  # type: ignore[arg-type]
        idempotency_key="key_01",
    )
    assert tr.status is ToolCallStatus.COMPLETED
    assert tr.side_effect is SideEffectType.READ_ONLY

    with pytest.raises(ValueError, match=r"ToolResult\.status must be a valid ToolCallStatus"):
        ToolResult(
            invocation_id=ToolInvocationId("inv_01"),
            tool_name="test_tool",
            status="not_a_status",  # type: ignore[arg-type]
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key="key_01",
        )


def test_m03_reject_lossy_coercion_and_invalid_primitive_types() -> None:
    """M-03 regression test: Booleans and invalid types rejected to prevent lossy conversions."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)

    # Boolean passed for float confidence is rejected
    with pytest.raises(TypeError, match=r"MemoryRecord\.confidence must be float"):
        MemoryRecord(
            id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.SEMANTIC,
            content="Fact",
            state=MemoryState.ACTIVE,
            confidence=True,
            salience=0.5,
            created_at=now,
            updated_at=now,
        )

    # Boolean passed for float salience is rejected
    with pytest.raises(TypeError, match=r"MemoryRecord\.salience must be float"):
        MemoryRecord(
            id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.SEMANTIC,
            content="Fact",
            state=MemoryState.ACTIVE,
            confidence=0.9,
            salience=False,
            created_at=now,
            updated_at=now,
        )

    # Boolean passed for integer access_count is rejected
    with pytest.raises(TypeError, match=r"MemoryRecord\.access_count must be int"):
        MemoryRecord(
            id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_01"),
            kind=MemoryKind.SEMANTIC,
            content="Fact",
            state=MemoryState.ACTIVE,
            confidence=0.9,
            salience=0.5,
            created_at=now,
            updated_at=now,
            access_count=True,
        )

    # Boolean passed for RunBudgets.max_steps is rejected
    with pytest.raises(TypeError, match=r"RunBudgets\.max_steps must be int"):
        RunBudgets(max_steps=True)

    # Boolean passed for TokenUsage counts is rejected
    with pytest.raises(TypeError, match=r"Token counts must be non-boolean integers"):
        TokenUsage(prompt_tokens=True, completion_tokens=10, total_tokens=11)


def test_memory_record_fail_closed_provenance_and_scoping() -> None:
    """Provenance must fail-closed to EXTERNAL_RETRIEVAL and run/branch scoping validated."""
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    raw_dict = {
        "id": "mem_prov_01",
        "namespace_id": "ns_tenant",
        "kind": "semantic",
        "content": "Secret knowledge",
        "state": "active",
        "confidence": 0.85,
        "salience": 0.75,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }

    # When origin is omitted, it fails closed to EXTERNAL_RETRIEVAL, never SYSTEM_PROVIDED
    rec_omitted = MemoryRecord.from_dict(raw_dict)
    assert rec_omitted.origin == MemoryOrigin.EXTERNAL_RETRIEVAL
    assert rec_omitted.run_id is None
    assert rec_omitted.branch_id is None

    # Explicit origin is respected
    rec_system = MemoryRecord.from_dict({**raw_dict, "origin": "system_provided"})
    assert rec_system.origin == MemoryOrigin.SYSTEM_PROVIDED

    # Malformed origin string is rejected
    with pytest.raises(ValueError, match=r"MemoryRecord\.origin must be a valid MemoryOrigin"):
        MemoryRecord.from_dict({**raw_dict, "origin": "untrusted_source"})

    # Invalid origin type is rejected
    with pytest.raises(
        TypeError, match=r"MemoryRecord\.origin must be an instance of MemoryOrigin or str"
    ):
        MemoryRecord.from_dict({**raw_dict, "origin": 42})

    # Scoping fields roundtrip successfully
    rec_scoped = MemoryRecord.from_dict(
        {**raw_dict, "run_id": "run_01", "branch_id": "branch_main"}
    )
    assert rec_scoped.run_id == RunId("run_01")
    assert rec_scoped.branch_id == BranchId("branch_main")
    roundtrip = MemoryRecord.from_dict(rec_scoped.to_dict())
    assert roundtrip.run_id == RunId("run_01")
    assert roundtrip.branch_id == BranchId("branch_main")

    # Empty run_id and branch_id are rejected when provided
    with pytest.raises(ValueError, match=r"MemoryRecord\.run_id must not be empty"):
        MemoryRecord(
            id=MemoryId("m_1"),
            namespace_id=NamespaceId("ns_1"),
            kind=MemoryKind.SEMANTIC,
            content="content",
            state=MemoryState.ACTIVE,
            confidence=0.5,
            salience=0.5,
            created_at=now,
            updated_at=now,
            run_id=RunId("   "),
        )

    with pytest.raises(ValueError, match=r"MemoryRecord\.branch_id must not be empty"):
        MemoryRecord(
            id=MemoryId("m_1"),
            namespace_id=NamespaceId("ns_1"),
            kind=MemoryKind.SEMANTIC,
            content="content",
            state=MemoryState.ACTIVE,
            confidence=0.5,
            salience=0.5,
            created_at=now,
            updated_at=now,
            branch_id=BranchId("   "),
        )


def test_memory_candidate_provenance_and_scoping() -> None:
    """MemoryCandidate provenance defaults to AGENT_OBSERVATION and scoping validated."""
    raw_dict = {
        "candidate_id": "cand_01",
        "namespace_id": "ns_tenant",
        "kind": "episodic",
        "content": "User prefers dark mode",
        "proposed_salience": 0.8,
        "source_step_id": "step_01",
        "rationale": "Direct preference statement",
    }

    # Omitted origin defaults to AGENT_OBSERVATION
    cand_default = MemoryCandidate.from_dict(raw_dict)
    assert cand_default.origin == MemoryOrigin.AGENT_OBSERVATION
    assert cand_default.run_id is None
    assert cand_default.branch_id is None

    # Explicit origin is respected
    cand_explicit = MemoryCandidate.from_dict({**raw_dict, "origin": "user_explicit"})
    assert cand_explicit.origin == MemoryOrigin.USER_EXPLICIT

    # Malformed origin rejected
    with pytest.raises(ValueError, match=r"MemoryCandidate\.origin must be a valid MemoryOrigin"):
        MemoryCandidate.from_dict({**raw_dict, "origin": "bogus"})

    # Invalid origin type rejected
    with pytest.raises(
        TypeError, match=r"MemoryCandidate\.origin must be an instance of MemoryOrigin or str"
    ):
        MemoryCandidate.from_dict({**raw_dict, "origin": False})

    # Scoping fields roundtrip
    cand_scoped = MemoryCandidate.from_dict(
        {**raw_dict, "run_id": "run_01", "branch_id": "branch_experiment"}
    )
    assert cand_scoped.run_id == RunId("run_01")
    assert cand_scoped.branch_id == BranchId("branch_experiment")
    roundtrip = MemoryCandidate.from_dict(cand_scoped.to_dict())
    assert roundtrip.run_id == RunId("run_01")
    assert roundtrip.branch_id == BranchId("branch_experiment")

    # Empty run_id and branch_id are rejected
    with pytest.raises(ValueError, match=r"MemoryCandidate\.run_id must not be empty"):
        MemoryCandidate(
            candidate_id="c_1",
            namespace_id=NamespaceId("ns_1"),
            kind=MemoryKind.EPISODIC,
            content="content",
            proposed_salience=0.5,
            source_step_id=StepId("step_1"),
            rationale="rationale",
            run_id=RunId("   "),
        )

    with pytest.raises(ValueError, match=r"MemoryCandidate\.branch_id must not be empty"):
        MemoryCandidate(
            candidate_id="c_1",
            namespace_id=NamespaceId("ns_1"),
            kind=MemoryKind.EPISODIC,
            content="content",
            proposed_salience=0.5,
            source_step_id=StepId("step_1"),
            rationale="rationale",
            branch_id=BranchId("   "),
        )


def test_storage_custom_exceptions() -> None:
    """Storage exceptions inherit properly and preserve diagnostic fields."""
    v_err = StorageVersionConflictError(
        "Version mismatch",
        record_id="mem_1",
        expected_version=1,
        actual_version=2,
    )
    assert isinstance(v_err, StorageError)
    assert v_err.error_code == "STORAGE_VERSION_CONFLICT"
    assert v_err.record_id == "mem_1"
    assert v_err.expected_version == 1
    assert v_err.actual_version == 2

    ns_err = CrossNamespaceAccessError(
        "Tenant violation",
        requested_namespace="ns_a",
        target_namespace="ns_b",
        resource_id="res_1",
    )
    assert isinstance(ns_err, StorageError)
    assert ns_err.error_code == "CROSS_NAMESPACE_ACCESS"
    assert ns_err.requested_namespace == "ns_a"
    assert ns_err.target_namespace == "ns_b"
    assert ns_err.resource_id == "res_1"

    rnf_err = RecordNotFoundError(
        "Missing record",
        record_id="mem_missing",
        namespace_id="ns_a",
        table="memory_records",
    )
    assert isinstance(rnf_err, StorageError)
    assert rnf_err.error_code == "RECORD_NOT_FOUND"

    tomb_err = TombstoneActiveError(
        "Memory tombstoned",
        target_memory_id="mem_tomb",
        namespace_id="ns_a",
        tombstone_id="tomb_1",
    )
    assert isinstance(tomb_err, StorageError)
    assert tomb_err.error_code == "TOMBSTONE_ACTIVE"


def test_scored_memory_item_validation_and_serialization() -> None:
    """ScoredMemoryItem validates its fields and roundtrips through JSON."""
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    rec = MemoryRecord(
        id=MemoryId("mem_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="Important fact",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
    )
    item = ScoredMemoryItem(
        record=rec,
        score=0.95,
        score_breakdown={"bm25": 0.8, "recency": 0.15},
    )
    assert item.score == 0.95
    assert item.score_breakdown["bm25"] == 0.8

    # Serialization roundtrip
    d = item.to_dict()
    from_d = ScoredMemoryItem.from_dict(d)
    assert from_d == item
    assert ScoredMemoryItem.from_json(item.to_json()) == item

    # Validation errors
    with pytest.raises(TypeError, match=r"ScoredMemoryItem\.record must be MemoryRecord"):
        ScoredMemoryItem(record="not_a_record", score=0.5)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match=r"ScoredMemoryItem\.score must be float"):
        ScoredMemoryItem(record=rec, score="not_a_float")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=r"ScoredMemoryItem\.score must be finite"):
        ScoredMemoryItem(record=rec, score=float("nan"))

    with pytest.raises(TypeError, match=r"score_breakdown values must be float"):
        ScoredMemoryItem(record=rec, score=0.5, score_breakdown={"k": "not_float"})  # type: ignore[dict-item]


def test_memory_filter_validation_and_serialization() -> None:
    """MemoryFilter validates boundaries and roundtrips through dict and JSON."""
    now = datetime(2026, 10, 14, 15, 0, 0, tzinfo=UTC)
    mf = MemoryFilter(
        namespace_id=NamespaceId("ns_prod"),
        kinds=(MemoryKind.EPISODIC, MemoryKind.SEMANTIC),
        run_id=RunId("run_1"),
        branch_id=BranchId("branch_main"),
        origin=MemoryOrigin.AGENT_OBSERVATION,
        state=MemoryState.ACTIVE,
        as_of=now,
        min_confidence=0.7,
        min_salience=0.5,
        query_text="user settings",
        limit=20,
    )
    assert mf.limit == 20
    assert len(mf.kinds) == 2

    # Serialization roundtrip
    d = mf.to_dict()
    from_d = MemoryFilter.from_dict(d)
    assert from_d == mf
    assert MemoryFilter.from_json(mf.to_json()) == mf

    # Empty namespace rejected
    with pytest.raises(ValueError, match=r"MemoryFilter\.namespace_id must not be empty"):
        MemoryFilter(namespace_id=NamespaceId("   "))

    # Naive as_of rejected
    with pytest.raises(ValueError, match=r"MemoryFilter\.as_of must be timezone-aware"):
        MemoryFilter(
            namespace_id=NamespaceId("ns_1"),
            as_of=datetime(2026, 1, 1, 0, 0),  # Naive
        )

    # Out of bounds confidence/salience rejected
    with pytest.raises(
        ValueError, match=r"MemoryFilter\.min_confidence must be between 0\.0 and 1\.0"
    ):
        MemoryFilter(namespace_id=NamespaceId("ns_1"), min_confidence=1.5)

    with pytest.raises(
        ValueError, match=r"MemoryFilter\.min_salience must be between 0\.0 and 1\.0"
    ):
        MemoryFilter(namespace_id=NamespaceId("ns_1"), min_salience=-0.1)

    # Invalid limit rejected
    with pytest.raises(ValueError, match=r"MemoryFilter\.limit must be > 0"):
        MemoryFilter(namespace_id=NamespaceId("ns_1"), limit=0)


def test_storage_port_protocol_conformance() -> None:
    """Verify mock implementation conforming to StoragePort and MemoryStoreProtocol."""

    class DummyStorage:
        def put(self, record: MemoryRecord) -> None:
            pass

        def get(self, memory_id: MemoryId, namespace_id: NamespaceId) -> MemoryRecord | None:
            return None

        def search(
            self,
            namespace_id: NamespaceId,
            query: str,
            kinds: Sequence[MemoryKind],
            limit: int,
        ) -> Sequence[MemoryRecord]:
            return ()

        def search_scored(self, query_filter: MemoryFilter) -> Sequence[ScoredMemoryItem]:
            return ()

        def list_active(
            self, namespace_id: NamespaceId, kind: MemoryKind
        ) -> Sequence[MemoryRecord]:
            return ()

        def tombstone(self, memory_id: MemoryId, namespace_id: NamespaceId, reason: str) -> None:
            pass

        def list_tombstoned_memory_ids(self, namespace_id: NamespaceId) -> frozenset[MemoryId]:
            return frozenset()

        def update_optimistic(self, record: MemoryRecord, expected_version: int) -> None:
            pass

        def save_run(self, run_result: RunResult) -> None:
            pass

        def get_run(self, run_id: RunId, namespace_id: NamespaceId) -> RunResult | None:
            return None

        def list_orphaned_runs(self, namespace_id: NamespaceId) -> Sequence[RunResult]:
            return ()

        def save_step(self, step_result: StepResult, namespace_id: NamespaceId) -> None:
            pass

        def record_step_start(
            self,
            step_id: StepId,
            run_id: RunId,
            namespace_id: NamespaceId,
            step_number: int,
            started_at: datetime,
        ) -> None:
            pass

        def record_tool_intent(
            self,
            invocation_id: ToolInvocationId,
            step_id: StepId,
            namespace_id: NamespaceId,
            tool_name: str,
            arguments: Mapping[str, Any],
            side_effect: SideEffectType,
            idempotency_key: str,
            dispatched_at: datetime,
        ) -> None:
            pass

        def record_tool_result(self, result: ToolResult, namespace_id: NamespaceId) -> None:
            pass

        def get_tool_invocation(
            self,
            invocation_id: ToolInvocationId,
            namespace_id: NamespaceId,
        ) -> ToolResult | None:
            return None

        def save_checkpoint(
            self,
            checkpoint_id: CheckpointId,
            run_id: RunId,
            step_id: StepId,
            branch_id: BranchId,
            namespace_id: NamespaceId,
            state_payload: Mapping[str, Any],
            created_at: datetime,
            parent_checkpoint_id: CheckpointId | None = None,
        ) -> None:
            pass

        def get_checkpoint(
            self,
            checkpoint_id: CheckpointId,
            namespace_id: NamespaceId,
        ) -> Mapping[str, Any] | None:
            return None

        def append_trace_event(
            self,
            event_id: EventId,
            run_id: RunId,
            namespace_id: NamespaceId,
            sequence_num: int,
            event_type: str,
            payload: Mapping[str, Any],
            emitted_at: datetime,
            step_id: StepId | None = None,
        ) -> None:
            pass

        def get_trace_events(
            self,
            run_id: RunId,
            namespace_id: NamespaceId,
        ) -> Sequence[Mapping[str, Any]]:
            return ()

    dummy = DummyStorage()
    assert isinstance(dummy, StoragePort)
    assert isinstance(dummy, MemoryStoreProtocol)


def test_storage_boundary_validation_adapters() -> None:
    """Storage adapters parse valid payloads and reject invalid inputs."""
    valid_filter_dict = {
        "namespace_id": "ns_test",
        "kinds": ["semantic"],
        "limit": 10,
    }
    mf = validate_memory_filter(valid_filter_dict)
    assert mf.namespace_id == NamespaceId("ns_test")
    assert mf.limit == 10

    with pytest.raises(BoundaryValidationError):
        validate_memory_filter({"namespace_id": "", "limit": -5})

    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    rec = MemoryRecord(
        id=MemoryId("mem_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="Important fact",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
    )
    valid_item_dict = {
        "record": rec.to_dict(),
        "score": 0.88,
        "score_breakdown": {"base": 0.88},
    }
    item = validate_scored_memory_item(valid_item_dict)
    assert item.score == 0.88
    assert item.record.id == MemoryId("mem_01")

    with pytest.raises(BoundaryValidationError):
        validate_scored_memory_item({"record": "bad", "score": "not_num"})


def test_schema_version_and_deterministic_state_hash() -> None:
    """Deterministic state hashing computes canonical SHA-256 and verifies replay match."""
    assert SCHEMA_VERSION == 1

    payload_a = {"b": 2, "a": 1, "nested": {"y": "yes", "x": "ex"}}
    payload_b = {"nested": {"x": "ex", "y": "yes"}, "a": 1, "b": 2}

    # Order invariance due to canonical JSON key sorting
    hash_a = compute_state_hash(payload_a)
    hash_b = compute_state_hash(payload_b)
    assert hash_a == hash_b
    assert len(hash_a) == 64

    # SerializableRecord hashing
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    rec = MemoryRecord(
        id=MemoryId("mem_hash_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="Important fact",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
    )
    rec_hash = compute_state_hash(rec)
    assert len(rec_hash) == 64

    # Verification passes on identical payload
    assert verify_state_hash(rec, rec_hash) is True
    assert verify_state_hash(payload_a, hash_a) is True

    # Verification divergence raises ReplayDivergenceError by default
    bogus_hash = "0" * 64
    with pytest.raises(ReplayDivergenceError) as exc_info:
        verify_state_hash(payload_a, bogus_hash)
    assert exc_info.value.expected_hash == bogus_hash
    assert exc_info.value.actual_hash == hash_a

    # Verification divergence returns False when raise_on_divergence is False
    assert verify_state_hash(payload_a, bogus_hash, raise_on_divergence=False) is False

    # Unsupported payload type raises TypeError
    with pytest.raises(TypeError, match=r"payload must be Mapping or SerializableRecord"):
        compute_state_hash([1, 2, 3])  # type: ignore[arg-type]

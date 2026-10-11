"""Tests for contract-level validation rules across public dataclasses."""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from memory_aware_agent_harness.contracts import (
    CandidateStatus,
    ContextManifest,
    MemoryCandidate,
    MemoryId,
    MemoryKind,
    MemoryRecord,
    MemoryState,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepId,
    StepResult,
    ToolCallStatus,
    ToolInvocationId,
    ToolResult,
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
            excluded_item_ids=MappingProxyType({}),
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

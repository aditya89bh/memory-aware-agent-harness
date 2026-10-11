"""Comprehensive negative-path, invalid deserialization, and FSM failure tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from memory_aware_agent_harness.contracts import (
    LEGAL_RUN_TRANSITIONS,
    LEGAL_STEP_TRANSITIONS,
    BudgetExceededError,
    CompensationFailedError,
    FailureDetails,
    HarnessError,
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    PolicyViolationError,
    ReplayDivergenceError,
    RunBudgets,
    RunId,
    RunStatus,
    SideEffectType,
    StepId,
    StepState,
    StorageError,
    TokenUsage,
    ToolCallContext,
    ToolCallRequest,
    ToolCallStatus,
    ToolExecutionError,
    ToolInvocationId,
    ToolResult,
)


def test_typed_exceptions_hierarchy() -> None:
    """All custom domain exceptions must inherit from HarnessError."""
    exceptions = [
        BudgetExceededError("Budget exceeded"),
        PolicyViolationError("Policy violation"),
        ToolExecutionError("Tool execution failed"),
        CompensationFailedError("Compensation failed"),
        ReplayDivergenceError("Replay diverged"),
        StorageError("Storage failure"),
    ]
    for exc in exceptions:
        assert isinstance(exc, HarnessError)
        with pytest.raises(HarnessError):
            raise exc


def test_malformed_json_deserialization_failures() -> None:
    """Invalid or malformed JSON payloads fail with appropriate exceptions."""
    # Syntax error
    with pytest.raises(json.JSONDecodeError):
        ToolCallRequest.from_json("{broken json")

    # Missing mandatory keys
    with pytest.raises(KeyError):
        ToolCallRequest.from_json('{"call_id": "c1"}')

    # Unknown enum string
    with pytest.raises(ValueError, match=r"is not a valid"):
        MemoryRecord.from_dict(
            {
                "id": "mem_01",
                "namespace_id": "ns_01",
                "kind": "invalid_kind_name",
                "content": "some content",
                "state": "active",
                "confidence": 0.8,
                "salience": 0.5,
                "created_at": "2026-10-11T12:00:00+00:00",
                "updated_at": "2026-10-11T12:00:00+00:00",
            }
        )


def test_memory_record_strict_boundary_failures() -> None:
    """Confidence and salience strictly reject out-of-bounds numbers."""
    now = datetime.now(UTC)
    base = {
        "id": MemoryId("mem_01"),
        "namespace_id": "ns_01",
        "kind": MemoryKind.SEMANTIC,
        "content": "fact",
        "state": MemoryState.ACTIVE,
        "created_at": now,
        "updated_at": now,
    }

    # Confidence upper and lower bounds
    with pytest.raises(ValueError, match=r"confidence must be between 0\.0 and 1\.0"):
        MemoryRecord(confidence=1.0001, salience=0.5, **base)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=r"confidence must be between 0\.0 and 1\.0"):
        MemoryRecord(confidence=-0.0001, salience=0.5, **base)  # type: ignore[arg-type]

    # Salience upper and lower bounds
    with pytest.raises(ValueError, match=r"salience must be between 0\.0 and 1\.0"):
        MemoryRecord(confidence=0.5, salience=1.01, **base)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=r"salience must be between 0\.0 and 1\.0"):
        MemoryRecord(confidence=0.5, salience=-0.01, **base)  # type: ignore[arg-type]

    # Naive timestamp in updated_at
    with pytest.raises(ValueError, match=r"updated_at must be timezone-aware"):
        MemoryRecord(
            confidence=0.5,
            salience=0.5,
            id=MemoryId("mem_01"),
            namespace_id="ns_01",  # type: ignore[arg-type]
            kind=MemoryKind.SEMANTIC,
            content="fact",
            state=MemoryState.ACTIVE,
            created_at=now,
            updated_at=datetime(2026, 1, 1, 0, 0),  # Naive
        )


def test_token_usage_arithmetic_discrepancy() -> None:
    """TokenUsage rejects negative token numbers and arithmetic discrepancies."""
    with pytest.raises(ValueError, match=r"Token counts must be non-negative"):
        TokenUsage(prompt_tokens=-10, completion_tokens=20, total_tokens=10)

    with pytest.raises(ValueError, match=r"Token counts must be non-negative"):
        TokenUsage(prompt_tokens=10, completion_tokens=-5, total_tokens=5)

    with pytest.raises(ValueError, match=r"total_tokens .* must equal prompt_tokens"):
        TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=35)


def test_illegal_run_transitions_rejected() -> None:
    """FSM legal transitions reject arbitrary illegal jumps."""
    # Terminal states can never transition to anything
    for terminal in (
        RunStatus.COMPLETED,
        RunStatus.BUDGET_EXHAUSTED,
        RunStatus.CANCELLED,
        RunStatus.ABORTED,
        RunStatus.FAILED,
    ):
        for candidate in RunStatus:
            assert candidate not in LEGAL_RUN_TRANSITIONS[terminal]

    # INITIALIZED cannot jump directly to COMPLETED or PAUSED
    assert RunStatus.COMPLETED not in LEGAL_RUN_TRANSITIONS[RunStatus.INITIALIZED]
    assert RunStatus.PAUSED not in LEGAL_RUN_TRANSITIONS[RunStatus.INITIALIZED]
    assert RunStatus.BUDGET_EXHAUSTED not in LEGAL_RUN_TRANSITIONS[RunStatus.INITIALIZED]


def test_illegal_step_transitions_rejected() -> None:
    """Step FSM transitions reject out-of-order phase execution."""
    # Step cannot skip from NOT_STARTED directly to COMMITTING_MEMORIES
    assert StepState.COMMITTING_MEMORIES not in LEGAL_STEP_TRANSITIONS[StepState.NOT_STARTED]
    assert StepState.CHECKPOINTING not in LEGAL_STEP_TRANSITIONS[StepState.RETRIEVING]

    # Cannot transition backward
    assert StepState.RETRIEVING not in LEGAL_STEP_TRANSITIONS[StepState.COMPLETED]
    assert StepState.DECIDING not in LEGAL_STEP_TRANSITIONS[StepState.CHECKPOINTING]

    # Terminal states have no transitions
    for term_step in (StepState.COMPLETED, StepState.FAILED, StepState.ABORTED):
        assert len(LEGAL_STEP_TRANSITIONS[term_step]) == 0


def test_orphaned_run_and_unknown_outcome_contract() -> None:
    """Test simulated recovery transitions for crashed or orphaned executions."""
    # An active run left in RUNNING on process crash transitions to FAILED or PAUSED
    crashed_status = RunStatus.RUNNING
    assert RunStatus.FAILED in LEGAL_RUN_TRANSITIONS[crashed_status]
    assert RunStatus.PAUSED in LEGAL_RUN_TRANSITIONS[crashed_status]

    # In-flight tool invocation with unconfirmed outcome transitions to OUTCOME_UNKNOWN
    t_res = ToolResult(
        invocation_id=ToolInvocationId("inv_crash"),
        tool_name="stripe_charge",
        status=ToolCallStatus.OUTCOME_UNKNOWN,
        side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
        idempotency_key="idemp_crash_1",
        error="Process terminated before tool execution confirmation",
    )
    assert t_res.status == ToolCallStatus.OUTCOME_UNKNOWN
    assert t_res.side_effect == SideEffectType.NON_IDEMPOTENT_WRITE

    # Failure diagnostics record context and hints
    diag = FailureDetails(
        error_type="OrphanedRunRecovery",
        message="Active run crashed mid-step during non-idempotent tool invocation",
        step_id=StepId("step_03"),
        recovery_hint="Check external idempotency key before resuming",
    )
    assert diag.step_id == "step_03"
    assert diag.recovery_hint is not None


def test_memory_origin_enumeration_coverage() -> None:
    """All 5 defined MemoryOrigin variants exist and serialize to strings."""
    origins = [
        MemoryOrigin.SYSTEM_PROVIDED,
        MemoryOrigin.USER_EXPLICIT,
        MemoryOrigin.AGENT_OBSERVATION,
        MemoryOrigin.DERIVED_CONSOLIDATION,
        MemoryOrigin.EXTERNAL_RETRIEVAL,
    ]
    for origin in origins:
        assert isinstance(origin.value, str)
        assert origin == MemoryOrigin(origin.value)


def test_context_validation_failures() -> None:
    """Contexts reject negative budgets or timeout bounds."""
    with pytest.raises(ValueError, match=r"max_steps must be > 0"):
        RunBudgets(max_steps=-1)

    with pytest.raises(ValueError, match=r"timeout_seconds must be > 0\.0"):
        ToolCallContext(
            run_id=RunId("r1"),
            step_id=StepId("s1"),
            namespace_id="n1",  # type: ignore[arg-type]
            invocation_id=ToolInvocationId("i1"),
            idempotency_key="k1",
            timeout_seconds=-0.5,
        )

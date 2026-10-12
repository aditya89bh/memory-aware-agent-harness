"""Unit tests for Pydantic v2 boundary validation layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from memory_aware_agent_harness.contracts import (
    CandidateStatus,
    MemoryKind,
    MemoryRecord,
    MemoryState,
    ModelFinishReason,
    RunBudgets,
    RunResult,
    RunStatus,
    SideEffectType,
    StepResult,
    TokenUsage,
    ToolCallStatus,
    ToolResult,
)
from memory_aware_agent_harness.validation import (
    BoundaryValidationError,
    get_adapter,
    validate_context_item,
    validate_context_manifest,
    validate_external_input,
    validate_failure_details,
    validate_memory_candidate,
    validate_memory_record,
    validate_model_decision,
    validate_run_budgets,
    validate_run_result,
    validate_step_result,
    validate_token_usage,
    validate_tool_call_request,
    validate_tool_result,
)


def test_validate_memory_record_from_dict() -> None:
    """Valid dict input successfully parses into an immutable MemoryRecord."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    payload = {
        "id": "mem_boundary_01",
        "namespace_id": "ns_boundary",
        "kind": "semantic",
        "content": "User preferences",
        "state": "active",
        "confidence": 0.95,
        "salience": 0.8,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    record = validate_memory_record(payload)
    assert isinstance(record, MemoryRecord)
    assert record.id == "mem_boundary_01"
    assert record.kind is MemoryKind.SEMANTIC
    assert record.state is MemoryState.ACTIVE
    assert record.confidence == 0.95


def test_validate_memory_record_from_json_string() -> None:
    """Valid JSON string parses into an immutable MemoryRecord."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    json_str = json.dumps(
        {
            "id": "mem_json_01",
            "namespace_id": "ns_json",
            "kind": "episodic",
            "content": "Interaction log",
            "state": "active",
            "confidence": 0.88,
            "salience": 0.75,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    )
    record = validate_memory_record(json_str)
    assert isinstance(record, MemoryRecord)
    assert record.id == "mem_json_01"
    assert record.kind is MemoryKind.EPISODIC


def test_validate_memory_record_invalid_json_raises_boundary_error() -> None:
    """Malformed JSON string raises BoundaryValidationError with structured details."""
    with pytest.raises(BoundaryValidationError) as exc_info:
        validate_memory_record("not valid json at all")

    assert exc_info.value.target_type == "MemoryRecord"
    assert exc_info.value.error_code == "BOUNDARY_VALIDATION_ERROR"
    assert "errors" in exc_info.value.details


def test_validate_memory_record_field_bounds_violation_raises() -> None:
    """Confidence out of range raises BoundaryValidationError."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    payload = {
        "id": "mem_invalid_conf",
        "namespace_id": "ns_01",
        "kind": "semantic",
        "content": "Fact",
        "state": "active",
        "confidence": 1.5,  # out of bounds [0.0, 1.0]
        "salience": 0.5,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    with pytest.raises(BoundaryValidationError) as exc_info:
        validate_memory_record(payload)

    assert exc_info.value.target_type == "MemoryRecord"
    assert len(exc_info.value.details["errors"]) > 0


def test_validate_run_budgets_from_bytes() -> None:
    """Valid UTF-8 bytes payload parses into RunBudgets."""
    raw_bytes = b'{"max_steps": 25, "max_tokens": 50000, "max_time_seconds": 180.0}'
    budgets = validate_run_budgets(raw_bytes)
    assert isinstance(budgets, RunBudgets)
    assert budgets.max_steps == 25
    assert budgets.max_tokens == 50000
    assert budgets.max_time_seconds == 180.0


def test_validate_run_budgets_invalid_raises() -> None:
    """Negative steps in RunBudgets raises BoundaryValidationError."""
    payload = {"max_steps": -1, "max_tokens": 1000, "max_time_seconds": 60.0}
    with pytest.raises(BoundaryValidationError) as exc_info:
        validate_run_budgets(payload)

    assert exc_info.value.target_type == "RunBudgets"


def test_validate_all_boundary_records() -> None:
    """Convenience validators cover all external boundary models."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)

    # 1. TokenUsage
    tu = validate_token_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    assert isinstance(tu, TokenUsage)

    # 2. FailureDetails
    fd = validate_failure_details({"error_type": "ValueError", "message": "Bad arg"})
    assert fd.error_type == "ValueError"

    # 3. ToolCallRequest
    tcr = validate_tool_call_request({"call_id": "c1", "tool_name": "calc", "arguments": {"x": 1}})
    assert tcr.call_id == "c1"

    # 4. ToolResult
    tres = validate_tool_result(
        {
            "invocation_id": "inv_1",
            "tool_name": "calc",
            "status": "completed",
            "side_effect": "read_only",
            "idempotency_key": "ik_1",
            "result": {"val": 42},
        }
    )
    assert isinstance(tres, ToolResult)
    assert tres.status is ToolCallStatus.COMPLETED
    assert tres.side_effect is SideEffectType.READ_ONLY

    # 5. ModelDecision
    dec = validate_model_decision(
        {
            "decision_id": "dec_1",
            "step_id": "step_1",
            "finish_reason": "stop",
            "tokens_used": 15,
            "token_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    assert dec.finish_reason is ModelFinishReason.STOP

    # 6. StepResult
    sres = validate_step_result(
        {
            "step_id": "step_1",
            "run_id": "run_1",
            "step_number": 1,
            "decision": {
                "decision_id": "dec_1",
                "step_id": "step_1",
                "finish_reason": "stop",
                "tokens_used": 15,
                "token_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
            "tool_results": (),
            "memories_proposed": 0,
            "memories_committed": 0,
            "tokens_consumed": 15,
            "completed_at": now.isoformat(),
        }
    )
    assert isinstance(sres, StepResult)
    assert sres.tokens_consumed == 15

    # 7. RunResult
    rres = validate_run_result(
        {
            "run_id": "run_1",
            "namespace_id": "ns_1",
            "status": "completed",
            "step_count": 1,
            "total_tokens_consumed": 15,
            "final_output": "Success",
            "trace_id": "tr_1",
            "token_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    assert isinstance(rres, RunResult)
    assert rres.status is RunStatus.COMPLETED

    # 8. MemoryCandidate
    mc = validate_memory_candidate(
        {
            "candidate_id": "cand_1",
            "namespace_id": "ns_1",
            "kind": "semantic",
            "content": "Fact",
            "proposed_salience": 0.8,
            "source_step_id": "step_1",
            "rationale": "High value",
        }
    )
    assert mc.validation_status is CandidateStatus.PENDING

    # 9. ContextItem
    ci = validate_context_item(
        {
            "memory_id": "m1",
            "kind": "semantic",
            "content": "Fact",
            "rank_score": 0.9,
            "provenance_id": "prov_1",
            "token_cost": 20,
        }
    )
    assert ci.rank_score == 0.9

    # 10. ContextManifest
    cman = validate_context_manifest(
        {
            "manifest_id": "man_1",
            "step_id": "step_1",
            "included_items": (),
            "excluded_item_ids": {},
            "total_tokens": 0,
            "budget_limit": 1000,
            "compiled_prompt": "Prompt text",
        }
    )
    assert cman.compiled_prompt == "Prompt text"


def test_get_adapter_is_cached() -> None:
    """TypeAdapter instances are cached across repeated calls."""
    ad1 = get_adapter(RunBudgets)
    ad2 = get_adapter(RunBudgets)
    assert ad1 is ad2


def test_generic_validate_external_input() -> None:
    """Generic validate_external_input works with any supported record."""
    usage = validate_external_input(
        TokenUsage, {"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75}
    )
    assert usage.total_tokens == 75

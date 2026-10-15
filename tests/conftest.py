"""Shared pytest fixtures and test configuration for memory-aware-agent-harness."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memory_aware_agent_harness.contracts import (
    ContextItem,
    ContextManifest,
    FrozenDict,
    MemoryId,
    MemoryKind,
    MemoryRecord,
    MemoryState,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    SideEffectType,
    StepId,
    ToolCallRequest,
    ToolCallStatus,
    ToolInvocationId,
    ToolResult,
)

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)


@pytest.fixture
def frozen_utc_now() -> datetime:
    """Deterministic timestamp fixture."""
    return datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def sample_memory_record(frozen_utc_now: datetime) -> MemoryRecord:
    """Fixture providing a valid immutable MemoryRecord."""
    return MemoryRecord(
        id=MemoryId("mem_01J9X001"),
        namespace_id=NamespaceId("test_tenant_a"),
        kind=MemoryKind.SEMANTIC,
        content="User prefers strict typing in Python 3.11+",
        state=MemoryState.ACTIVE,
        confidence=0.95,
        salience=0.8,
        created_at=frozen_utc_now,
        updated_at=frozen_utc_now,
        expires_at=None,
        source_references=("step_01J9X000",),
        metadata=FrozenDict({"category": "preference"}),
        access_count=1,
        version=1,
    )


@pytest.fixture
def sample_context_manifest() -> ContextManifest:
    """Fixture providing an immutable ContextManifest."""
    item = ContextItem(
        memory_id=MemoryId("mem_01J9X001"),
        kind=MemoryKind.SEMANTIC,
        content="User prefers strict typing in Python 3.11+",
        rank_score=0.92,
        provenance_id="step_01J9X000",
        token_cost=15,
    )
    prompt = "<untrusted_memory id='mem_01J9X001'>User prefers strict typing</untrusted_memory>"
    return ContextManifest(
        manifest_id="man_01J9X002",
        step_id=StepId("step_01J9X001"),
        included_items=(item,),
        excluded_item_ids=FrozenDict({MemoryId("mem_01J9X999"): "EXCLUDED_BUDGET_EXCEEDED"}),
        total_tokens=15,
        budget_limit=2000,
        compiled_prompt=prompt,
    )


@pytest.fixture
def sample_tool_result() -> ToolResult:
    """Fixture providing an immutable ToolResult."""
    return ToolResult(
        invocation_id=ToolInvocationId("inv_01J9X003"),
        tool_name="get_file_info",
        status=ToolCallStatus.COMPLETED,
        side_effect=SideEffectType.READ_ONLY,
        idempotency_key="idemp_key_123",
        result=FrozenDict({"file_size": 1024}),
        error=None,
        execution_time_ms=1.25,
    )


@pytest.fixture
def sample_model_decision() -> ModelDecision:
    """Fixture providing an immutable ModelDecision with a tool call."""
    call = ToolCallRequest(
        call_id="call_01J9X004",
        tool_name="get_file_info",
        arguments=FrozenDict({"path": "SPEC.md"}),
    )
    return ModelDecision(
        decision_id="dec_01J9X005",
        step_id=StepId("step_01J9X001"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        content=None,
        tool_calls=(call,),
        tokens_used=42,
        raw_response=FrozenDict({"model": "fake-deterministic"}),
    )


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Temporary SQLite database path for isolated storage tests."""
    return tmp_path / "test_harness.db"

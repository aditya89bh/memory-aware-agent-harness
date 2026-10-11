"""Smoke and integrity tests for public typing contracts, data models, and immutability."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from memory_aware_agent_harness.contracts import (
    BudgetExceededError,
    CompensationFailedError,
    ContextItem,
    ContextManifest,
    HarnessError,
    MemoryKind,
    MemoryRecord,
    MemoryState,
    ModelDecision,
    ModelFinishReason,
    PolicyViolationError,
    ReplayDivergenceError,
    RunStatus,
    SideEffectType,
    StorageError,
    ToolCallRequest,
    ToolCallStatus,
    ToolExecutionError,
    ToolResult,
)


def test_package_version_exposed() -> None:
    """Verify package version is properly exposed at package root."""
    import memory_aware_agent_harness

    assert memory_aware_agent_harness.__version__ == "0.1.0.dev0"


def test_memory_record_deep_immutability(sample_memory_record: MemoryRecord) -> None:
    """Verify MemoryRecord cannot be mutated at runtime."""
    with pytest.raises(FrozenInstanceError):
        # Attribute assignment must raise FrozenInstanceError
        sample_memory_record.content = "mutated content"  # type: ignore[misc]

    # Verify tuple collections cannot be appended
    assert isinstance(sample_memory_record.source_references, tuple)

    # Verify metadata is a mapping proxy and cannot be modified
    assert isinstance(sample_memory_record.metadata, MappingProxyType)
    with pytest.raises(TypeError):
        sample_memory_record.metadata["new_key"] = "forbidden"  # type: ignore[index]


def test_context_manifest_immutability(sample_context_manifest: ContextManifest) -> None:
    """Verify ContextManifest enforces immutability and bounds."""
    with pytest.raises(FrozenInstanceError):
        sample_context_manifest.budget_limit = 9999  # type: ignore[misc]

    assert len(sample_context_manifest.included_items) == 1
    item: ContextItem = sample_context_manifest.included_items[0]
    assert item.memory_id == "mem_01J9X001"
    assert item.rank_score == 0.92
    assert "mem_01J9X999" in sample_context_manifest.excluded_item_ids


def test_tool_result_immutability(sample_tool_result: ToolResult) -> None:
    """Verify ToolResult cannot be modified and preserves side effect type."""
    with pytest.raises(FrozenInstanceError):
        sample_tool_result.status = ToolCallStatus.FAILED  # type: ignore[misc]

    assert sample_tool_result.side_effect == SideEffectType.READ_ONLY
    assert sample_tool_result.result is not None
    assert sample_tool_result.result["file_size"] == 1024


def test_model_decision_structure(sample_model_decision: ModelDecision) -> None:
    """Verify ModelDecision tool calls and tokens accounting."""
    assert sample_model_decision.finish_reason == ModelFinishReason.TOOL_CALL
    assert len(sample_model_decision.tool_calls) == 1
    call: ToolCallRequest = sample_model_decision.tool_calls[0]
    assert call.tool_name == "get_file_info"
    assert call.arguments["path"] == "SPEC.md"
    assert sample_model_decision.tokens_used == 42


def test_enum_string_values() -> None:
    """Verify enum string representations match architectural expectations."""
    assert RunStatus.INITIALIZED.value == "initialized"
    assert RunStatus.RUNNING.value == "running"
    assert MemoryKind.EPISODIC.value == "episodic"
    assert MemoryKind.SEMANTIC.value == "semantic"
    assert MemoryKind.PROCEDURAL.value == "procedural"
    assert MemoryKind.WORKING.value == "working"
    assert MemoryState.ACTIVE.value == "active"
    assert MemoryState.DELETED.value == "deleted"
    assert SideEffectType.NON_IDEMPOTENT_WRITE.value == "non_idempotent_write"


def test_exception_hierarchy() -> None:
    """Verify all custom exceptions inherit from HarnessError."""
    assert issubclass(BudgetExceededError, HarnessError)
    assert issubclass(PolicyViolationError, HarnessError)
    assert issubclass(ToolExecutionError, HarnessError)
    assert issubclass(CompensationFailedError, HarnessError)
    assert issubclass(ReplayDivergenceError, HarnessError)
    assert issubclass(StorageError, HarnessError)

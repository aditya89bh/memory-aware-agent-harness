"""Tests for deep immutability, alias-mutation safety, and canonical serialization."""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    CandidateStatus,
    ContextItem,
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
    ToolCallRequest,
    ToolCallStatus,
    ToolInvocationId,
    ToolResult,
)


def _attempt_setitem(target: Any, key: Any, val: Any) -> None:
    target[key] = val


def test_tool_call_request_alias_mutation_defense() -> None:
    """Modifying arguments dict after passing it to ToolCallRequest does not mutate record."""
    raw_args: dict[str, Any] = {"nested": {"key": "original_val"}, "list_key": [1, 2, 3]}
    req = ToolCallRequest(call_id="call_01", tool_name="bash", arguments=raw_args)

    # Mutate original caller structures
    raw_args["nested"]["key"] = "hacked"
    raw_args["list_key"].append(4)

    # Verify record remains unchanged and frozen
    assert req.arguments["nested"]["key"] == "original_val"
    assert req.arguments["list_key"] == (1, 2, 3)

    # Verify attempting to mutate record's internal mapping raises TypeError
    with pytest.raises(TypeError):
        _attempt_setitem(req.arguments, "new_key", "forbidden")

    with pytest.raises(TypeError):
        _attempt_setitem(req.arguments["nested"], "key", "forbidden")


def test_memory_record_alias_mutation_defense() -> None:
    """Modifying metadata or source_references after instantiation does not mutate record."""
    raw_meta: dict[str, Any] = {"tags": ["auth", "security"], "scores": {"relevance": 0.95}}
    raw_refs = ["doc_ref_1", "doc_ref_2"]
    now = datetime.now(UTC)

    rec = MemoryRecord(
        id=MemoryId("mem_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="Secret credential policy",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
        source_references=tuple(raw_refs),
        metadata=raw_meta,
    )

    # Mutate caller data
    raw_meta["tags"].append("malicious")
    raw_meta["scores"]["relevance"] = 0.0
    raw_refs.append("fake_ref")

    # Record data remains uncorrupted
    assert rec.metadata["tags"] == ("auth", "security")
    assert rec.metadata["scores"]["relevance"] == 0.95
    assert rec.source_references == ("doc_ref_1", "doc_ref_2")

    # Direct mutation on record fails
    with pytest.raises(TypeError):
        _attempt_setitem(rec.metadata, "tags", ("new",))

    assert not hasattr(rec.source_references, "append")
    with pytest.raises(TypeError):
        _attempt_setitem(rec.source_references, 0, "fake")


def test_context_manifest_alias_mutation_defense() -> None:
    """ContextManifest collections cannot be mutated via aliases."""
    item = ContextItem(
        memory_id=MemoryId("mem_01"),
        kind=MemoryKind.SEMANTIC,
        content="Important fact",
        rank_score=0.9,
        provenance_id="prov_01",
        token_cost=10,
    )
    items_list = [item]
    excluded_dict = {MemoryId("mem_02"): "BUDGET_EXCEEDED"}

    manifest = ContextManifest(
        manifest_id="man_01",
        step_id=StepId("step_01"),
        included_items=tuple(items_list),
        excluded_item_ids=excluded_dict,
        total_tokens=10,
        budget_limit=1000,
        compiled_prompt="System prompt",
    )

    # Mutate caller collections
    items_list.append(item)
    excluded_dict[MemoryId("mem_03")] = "DUPLICATE"

    # Manifest collections remain unmodified
    assert len(manifest.included_items) == 1
    assert len(manifest.excluded_item_ids) == 1
    assert MemoryId("mem_03") not in manifest.excluded_item_ids


def test_tool_call_request_serialization_roundtrip() -> None:
    """ToolCallRequest serializes to canonical JSON and roundtrips perfectly."""
    req = ToolCallRequest(
        call_id="call_99",
        tool_name="database_query",
        arguments=MappingProxyType({"query": "SELECT 1", "limit": 10}),
    )
    d = req.to_dict()
    assert d == {
        "call_id": "call_99",
        "tool_name": "database_query",
        "arguments": {"query": "SELECT 1", "limit": 10},
    }

    json_str = req.to_json()
    assert '"call_id":"call_99"' in json_str

    req_deserialized = ToolCallRequest.from_json(json_str)
    assert req_deserialized == req


def test_memory_record_serialization_roundtrip() -> None:
    """MemoryRecord roundtrips through dict and json, preserving ISO timestamps and Enums."""
    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
    rec = MemoryRecord(
        id=MemoryId("mem_rec_01"),
        namespace_id=NamespaceId("ns_tenant"),
        kind=MemoryKind.PROCEDURAL,
        content="Follow step A before step B",
        state=MemoryState.ACTIVE,
        confidence=0.99,
        salience=0.85,
        created_at=now,
        updated_at=now,
        expires_at=now,
        source_references=("guideline_1", "manual_2"),
        metadata={"priority": "high", "flags": [1, 2]},
        access_count=5,
        version=2,
    )

    d = rec.to_dict()
    assert d["kind"] == "procedural"
    assert d["state"] == "active"
    assert d["created_at"] == "2026-10-11T12:00:00+00:00"
    assert d["source_references"] == ["guideline_1", "manual_2"]
    assert d["metadata"] == {"flags": [1, 2], "priority": "high"}

    json_str = rec.to_json()
    roundtrip = MemoryRecord.from_json(json_str)
    assert roundtrip == rec
    assert roundtrip.to_json() == json_str

    # Test roundtrip with 'Z' suffix string format
    d_with_z = dict(d)
    d_with_z["created_at"] = "2026-10-11T12:00:00Z"
    d_with_z["updated_at"] = "2026-10-11T12:00:00Z"
    d_with_z["expires_at"] = "2026-10-11T12:00:00Z"
    rec_from_z = MemoryRecord.from_dict(d_with_z)
    assert rec_from_z.created_at == now


def test_all_contract_records_json_roundtrip() -> None:
    """All public contract records cleanly roundtrip through canonical JSON."""
    now = datetime(2026, 10, 11, 15, 30, 0, tzinfo=UTC)

    # 1. ToolCallRequest
    tcr = ToolCallRequest(call_id="c_1", tool_name="search", arguments={"q": "foo"})
    assert ToolCallRequest.from_json(tcr.to_json()) == tcr

    # 2. ModelDecision
    dec = ModelDecision(
        decision_id="dec_1",
        step_id=StepId("step_1"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        content="Calling search",
        tool_calls=(tcr,),
        tokens_used=42,
        raw_response={"model": "gpt-4"},
    )
    assert ModelDecision.from_json(dec.to_json()) == dec

    # 3. ToolResult
    tres = ToolResult(
        invocation_id=ToolInvocationId("inv_1"),
        tool_name="search",
        status=ToolCallStatus.COMPLETED,
        side_effect=SideEffectType.READ_ONLY,
        idempotency_key="ik_1",
        result={"hits": [1, 2]},
        execution_time_ms=12.5,
    )
    assert ToolResult.from_json(tres.to_json()) == tres

    # 4. MemoryRecord
    mem = MemoryRecord(
        id=MemoryId("m_1"),
        namespace_id=NamespaceId("ns_1"),
        kind=MemoryKind.EPISODIC,
        content="Ran search",
        state=MemoryState.ACTIVE,
        confidence=1.0,
        salience=0.5,
        created_at=now,
        updated_at=now,
    )
    assert MemoryRecord.from_json(mem.to_json()) == mem

    # 5. MemoryCandidate
    cand = MemoryCandidate(
        candidate_id="cand_1",
        namespace_id=NamespaceId("ns_1"),
        kind=MemoryKind.EPISODIC,
        content="Candidate fact",
        proposed_salience=0.6,
        source_step_id=StepId("step_1"),
        rationale="User noted preference",
        validation_status=CandidateStatus.APPROVED,
    )
    assert MemoryCandidate.from_json(cand.to_json()) == cand

    # 6. ContextItem
    ci = ContextItem(
        memory_id=MemoryId("m_1"),
        kind=MemoryKind.EPISODIC,
        content="Ran search",
        rank_score=0.88,
        provenance_id="prov_1",
        token_cost=5,
    )
    assert ContextItem.from_json(ci.to_json()) == ci

    # 7. ContextManifest
    cm = ContextManifest(
        manifest_id="cm_1",
        step_id=StepId("step_1"),
        included_items=(ci,),
        excluded_item_ids={MemoryId("m_2"): "SCORE_TOO_LOW"},
        total_tokens=5,
        budget_limit=100,
        compiled_prompt="Context prompt",
    )
    assert ContextManifest.from_json(cm.to_json()) == cm

    # 8. StepResult
    sr = StepResult(
        step_id=StepId("step_1"),
        run_id=RunId("run_1"),
        step_number=1,
        decision=dec,
        tool_results=(tres,),
        memories_proposed=1,
        memories_committed=1,
        tokens_consumed=42,
        completed_at=now,
    )
    assert StepResult.from_json(sr.to_json()) == sr

    # 9. RunResult
    rr = RunResult(
        run_id=RunId("run_1"),
        namespace_id=NamespaceId("ns_1"),
        status=RunStatus.COMPLETED,
        step_count=1,
        total_tokens_consumed=42,
        final_output="Task finished successfully",
        trace_id="tr_1",
    )
    assert RunResult.from_json(rr.to_json()) == rr

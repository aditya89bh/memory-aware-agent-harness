"""Tests for deep immutability, alias-mutation safety, and canonical serialization."""

from __future__ import annotations

import copy
import dataclasses
import pickle
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    CandidateStatus,
    ContextItem,
    ContextManifest,
    FrozenDict,
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
        arguments=FrozenDict({"query": "SELECT 1", "limit": 10}),
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


def test_default_memory_record_supports_hash() -> None:
    """1. Default MemoryRecord supports hash() and can be used in sets and dict keys."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    rec = MemoryRecord(
        id=MemoryId("mem_hash_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="Hash support test",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
    )
    rec_hash = hash(rec)
    assert isinstance(rec_hash, int)
    record_set = {rec}
    assert rec in record_set
    record_map = {rec: "verified"}
    assert record_map[rec] == "verified"


def test_default_memory_record_supports_deepcopy() -> None:
    """2. Default MemoryRecord supports copy.deepcopy()."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    rec = MemoryRecord(
        id=MemoryId("mem_copy_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="Deepcopy support test",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
        metadata={"nested": {"flag": True, "count": 5}},
    )
    copied = copy.deepcopy(rec)
    assert copied == rec
    assert copied is not rec
    assert isinstance(copied.metadata, FrozenDict)
    assert hash(copied) == hash(rec)


def test_default_memory_record_supports_pickle() -> None:
    """3. Default MemoryRecord supports pickle.dumps()/loads()."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    rec = MemoryRecord(
        id=MemoryId("mem_pickle_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="Pickle support test",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
        metadata={"tags": ["a", "b"], "config": {"active": True}},
    )
    pickled = pickle.dumps(rec)
    unpickled = pickle.loads(pickled)
    assert unpickled == rec
    assert isinstance(unpickled.metadata, FrozenDict)
    assert hash(unpickled) == hash(rec)


def test_default_memory_record_supports_dataclasses_asdict() -> None:
    """4. Default MemoryRecord supports dataclasses.asdict()."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    rec = MemoryRecord(
        id=MemoryId("mem_asdict_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="asdict support test",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
        metadata={"category": "test"},
    )
    res = dataclasses.asdict(rec)
    assert isinstance(res, dict)
    assert res["id"] == "mem_asdict_01"
    assert isinstance(res["metadata"], dict)
    assert res["metadata"] == {"category": "test"}


def test_caller_mutation_cannot_change_nested_record_state_acceptance() -> None:
    """5. Caller mutation cannot change nested record state."""
    raw_meta: dict[str, Any] = {"nested": {"k": "v"}, "items": [1, 2]}
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    rec = MemoryRecord(
        id=MemoryId("mem_mut_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="Mutation defense test",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
        metadata=raw_meta,
    )
    raw_meta["nested"]["k"] = "corrupted"
    raw_meta["items"].append(3)
    assert rec.metadata["nested"]["k"] == "v"
    assert rec.metadata["items"] == (1, 2)

    assert isinstance(rec.metadata, FrozenDict)
    with pytest.raises(TypeError, match="FrozenDict is immutable"):
        _attempt_setitem(rec.metadata, "new_key", "value")
    with pytest.raises(TypeError, match="FrozenDict is immutable"):
        _attempt_setitem(rec.metadata["nested"], "k", "hacked")
    with pytest.raises(TypeError, match="FrozenDict is immutable"):
        rec.metadata.clear()
    with pytest.raises(TypeError, match="FrozenDict is immutable"):
        rec.metadata.pop("nested")
    with pytest.raises(TypeError, match="FrozenDict is immutable"):
        rec.metadata.update({"other": 1})


def test_canonical_json_identical_for_equivalent_utc_instants() -> None:
    """6. Canonical JSON produces identical output for equivalent UTC instants."""
    instant_utc = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
    instant_est = datetime(2026, 10, 12, 7, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    instant_ist = datetime(2026, 10, 12, 17, 30, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    rec_utc = MemoryRecord(
        id=MemoryId("mem_eq_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="UTC equivalence test",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=instant_utc,
        updated_at=instant_utc,
    )
    rec_est = MemoryRecord(
        id=MemoryId("mem_eq_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="UTC equivalence test",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=instant_est,
        updated_at=instant_est,
    )
    rec_ist = MemoryRecord(
        id=MemoryId("mem_eq_01"),
        namespace_id=NamespaceId("ns_01"),
        kind=MemoryKind.SEMANTIC,
        content="UTC equivalence test",
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=instant_ist,
        updated_at=instant_ist,
    )

    assert rec_utc.to_json() == rec_est.to_json() == rec_ist.to_json()


def test_nan_and_infinity_rejected() -> None:
    """7. NaN and Infinity are rejected."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="confidence must be finite"):
        MemoryRecord(
            id=MemoryId("m"),
            namespace_id=NamespaceId("ns"),
            kind=MemoryKind.SEMANTIC,
            content="text",
            state=MemoryState.ACTIVE,
            confidence=float("nan"),
            salience=0.5,
            created_at=now,
            updated_at=now,
        )
    with pytest.raises(ValueError, match="confidence must be finite"):
        MemoryRecord(
            id=MemoryId("m"),
            namespace_id=NamespaceId("ns"),
            kind=MemoryKind.SEMANTIC,
            content="text",
            state=MemoryState.ACTIVE,
            confidence=float("inf"),
            salience=0.5,
            created_at=now,
            updated_at=now,
        )
    with pytest.raises(ValueError, match="salience must be finite"):
        MemoryRecord(
            id=MemoryId("m"),
            namespace_id=NamespaceId("ns"),
            kind=MemoryKind.SEMANTIC,
            content="text",
            state=MemoryState.ACTIVE,
            confidence=0.5,
            salience=float("nan"),
            created_at=now,
            updated_at=now,
        )

    with pytest.raises(ValueError, match="Float value must be finite"):
        MemoryRecord(
            id=MemoryId("m"),
            namespace_id=NamespaceId("ns"),
            kind=MemoryKind.SEMANTIC,
            content="text",
            state=MemoryState.ACTIVE,
            confidence=0.5,
            salience=0.5,
            created_at=now,
            updated_at=now,
            metadata={"nan_val": float("nan")},
        )
    with pytest.raises(ValueError, match="Float value must be finite"):
        MemoryRecord(
            id=MemoryId("m"),
            namespace_id=NamespaceId("ns"),
            kind=MemoryKind.SEMANTIC,
            content="text",
            state=MemoryState.ACTIVE,
            confidence=0.5,
            salience=0.5,
            created_at=now,
            updated_at=now,
            metadata={"inf_val": float("inf")},
        )

    with pytest.raises(ValueError, match="rank_score must be finite"):
        ContextItem(
            memory_id=MemoryId("m"),
            kind=MemoryKind.SEMANTIC,
            content="text",
            rank_score=float("nan"),
            provenance_id="p",
            token_cost=10,
        )

    with pytest.raises(ValueError, match="execution_time_ms must be finite"):
        ToolResult(
            invocation_id=ToolInvocationId("inv"),
            tool_name="tool",
            status=ToolCallStatus.COMPLETED,
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key="k",
            execution_time_ms=float("nan"),
        )


def test_unsupported_metadata_values_rejected_instead_of_silently_converted() -> None:
    """8. Unsupported metadata values are rejected instead of silently converted."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)

    with pytest.raises(TypeError, match="Unsupported metadata value"):
        MemoryRecord(
            id=MemoryId("m"),
            namespace_id=NamespaceId("ns"),
            kind=MemoryKind.SEMANTIC,
            content="text",
            state=MemoryState.ACTIVE,
            confidence=0.5,
            salience=0.5,
            created_at=now,
            updated_at=now,
            metadata={"func": lambda: None},
        )

    with pytest.raises(TypeError, match="Unsupported metadata value"):
        MemoryRecord(
            id=MemoryId("m"),
            namespace_id=NamespaceId("ns"),
            kind=MemoryKind.SEMANTIC,
            content="text",
            state=MemoryState.ACTIVE,
            confidence=0.5,
            salience=0.5,
            created_at=now,
            updated_at=now,
            metadata={"raw_obj": object()},
        )

    with pytest.raises(TypeError, match="Mapping keys must be strings"):
        MemoryRecord(
            id=MemoryId("m"),
            namespace_id=NamespaceId("ns"),
            kind=MemoryKind.SEMANTIC,
            content="text",
            state=MemoryState.ACTIVE,
            confidence=0.5,
            salience=0.5,
            created_at=now,
            updated_at=now,
            metadata={123: "non_string_key"},  # type: ignore[dict-item]
        )

    with pytest.raises(ValueError, match="Datetime must be timezone-aware"):
        MemoryRecord(
            id=MemoryId("m"),
            namespace_id=NamespaceId("ns"),
            kind=MemoryKind.SEMANTIC,
            content="text",
            state=MemoryState.ACTIVE,
            confidence=0.5,
            salience=0.5,
            created_at=now,
            updated_at=now,
            metadata={"naive": datetime(2026, 1, 1, 12, 0, 0)},
        )


def test_to_dict_from_dict_and_to_json_from_json_roundtrip_accurately() -> None:
    """9. to_dict/from_dict and to_json/from_json round-trip accurately across structures."""
    now = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    rec = MemoryRecord(
        id=MemoryId("mem_roundtrip"),
        namespace_id=NamespaceId("ns_roundtrip"),
        kind=MemoryKind.SEMANTIC,
        content="Roundtrip accuracy verification",
        state=MemoryState.ACTIVE,
        confidence=0.88,
        salience=0.77,
        created_at=now,
        updated_at=now,
        expires_at=now,
        source_references=("ref_1", "ref_2"),
        metadata={"int": 1, "float": 2.5, "bool": True, "nested": {"str": "hello"}},
        access_count=3,
        version=2,
    )
    d = rec.to_dict()
    from_d = MemoryRecord.from_dict(d)
    assert from_d == rec

    j = rec.to_json()
    from_j = MemoryRecord.from_json(j)
    assert from_j == rec

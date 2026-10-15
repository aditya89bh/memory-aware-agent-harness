"""Regression tests for per-run checkpoint lineage isolation (P2-H3)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    CheckpointId,
    CrossNamespaceAccessError,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    RunResult,
    SideEffectType,
    StepId,
    TokenUsage,
    ToolCallRequest,
    ToolProtocol,
)
from memory_aware_agent_harness.doubles import DeterministicClock
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter

_BRANCH = "main"


class _ReadTool(ToolProtocol):
    name = "lookup"
    description = "read only"
    side_effect = SideEffectType.READ_ONLY

    def get_schema(self) -> dict[str, Any]:
        return {}

    def execute(self, arguments: Any, context: Any) -> dict[str, Any]:
        return {"ok": True}


class _ToggleModel(FakeModelAdapter):
    """Fake model that can be switched into a failing mode."""

    fail = False

    def decide(self, *args: Any, **kwargs: Any) -> ModelDecision:
        if self.fail:
            raise RuntimeError("synthetic model outage")
        return super().decide(*args, **kwargs)


def _two_step_script(tag: str) -> list[ModelDecision]:
    usage = TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    return [
        ModelDecision(
            decision_id=f"{tag}_1",
            step_id=StepId(f"{tag}_s1"),
            finish_reason=ModelFinishReason.TOOL_CALL,
            tool_calls=(ToolCallRequest(call_id=f"{tag}_c1", tool_name="lookup", arguments={}),),
            token_usage=usage,
        ),
        ModelDecision(
            decision_id=f"{tag}_2",
            step_id=StepId(f"{tag}_s2"),
            finish_reason=ModelFinishReason.STOP,
            content="done",
            token_usage=usage,
        ),
    ]


def _build() -> tuple[SQLiteStorageAdapter, AgentRuntime, _ToggleModel]:
    clock = DeterministicClock(datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC))
    storage = SQLiteStorageAdapter(":memory:", clock=clock.now)
    model = _ToggleModel()
    runtime = AgentRuntime(model=model, tools=[_ReadTool()], storage=storage, clock=clock)
    return storage, runtime, model


def _run(runtime: AgentRuntime, model: _ToggleModel, ns: str, tag: str) -> RunResult:
    for decision in _two_step_script(tag):
        model.queue_response(decision)
    return runtime.run(f"task {tag}", namespace_id=NamespaceId(ns))


def _checkpoint_rows(storage: SQLiteStorageAdapter) -> list[dict[str, Any]]:
    rows = storage.conn.execute(
        "SELECT checkpoint_id, run_id, namespace_id, parent_checkpoint_id "
        "FROM checkpoints ORDER BY created_at, rowid;"
    ).fetchall()
    return [dict(r) for r in rows]


def test_runtime_has_no_shared_checkpoint_head() -> None:
    storage, runtime, _ = _build()
    with storage:
        assert not hasattr(runtime, "_last_checkpoint_id")


def test_alternating_namespaces_keep_lineage_independent() -> None:
    storage, runtime, model = _build()
    with storage:
        results = [
            _run(runtime, model, "ns_a", "r1"),
            _run(runtime, model, "ns_b", "r2"),
            _run(runtime, model, "ns_a", "r3"),
            _run(runtime, model, "ns_b", "r4"),
        ]
        rows = _checkpoint_rows(storage)
        by_id = {r["checkpoint_id"]: r for r in rows}
        assert len(rows) == 8

        for res in results:
            run_rows = [r for r in rows if r["run_id"] == str(res.run_id)]
            assert len(run_rows) == 2
            first, second = run_rows
            assert first["parent_checkpoint_id"] is None
            assert second["parent_checkpoint_id"] == first["checkpoint_id"]

        for r in rows:
            parent = r["parent_checkpoint_id"]
            if parent is not None:
                assert by_id[parent]["run_id"] == r["run_id"]
                assert by_id[parent]["namespace_id"] == r["namespace_id"]


def test_failed_run_does_not_poison_following_run() -> None:
    storage, runtime, model = _build()
    with storage:
        _run(runtime, model, "ns_a", "ok1")
        model.fail = True
        with contextlib.suppress(Exception):
            _run(runtime, model, "ns_a", "bad")
        model.fail = False
        # Drain any scripted responses left unconsumed by the failed run.
        model._responses.clear()
        after = _run(runtime, model, "ns_a", "ok2")

        rows = [r for r in _checkpoint_rows(storage) if r["run_id"] == str(after.run_id)]
        assert len(rows) == 2
        assert rows[0]["parent_checkpoint_id"] is None
        assert rows[1]["parent_checkpoint_id"] == rows[0]["checkpoint_id"]


def test_storage_rejects_cross_run_parent() -> None:
    storage, runtime, model = _build()
    with storage:
        a = _run(runtime, model, "ns_a", "ra")
        b = _run(runtime, model, "ns_a", "rb")
        a_ckpt = storage.get_latest_checkpoint(a.run_id, NamespaceId("ns_a"))
        assert a_ckpt is not None

        with pytest.raises(ValueError, match="belongs to run"):
            storage.save_checkpoint(
                checkpoint_id=CheckpointId("ckpt_forged"),
                run_id=b.run_id,
                step_id=b.step_history[0].step_id,
                branch_id=a_ckpt.branch_id,
                namespace_id=NamespaceId("ns_a"),
                state_payload={"k": "v"},
                created_at=datetime(2026, 10, 15, tzinfo=UTC),
                parent_checkpoint_id=a_ckpt.checkpoint_id,
            )
        assert storage.get_checkpoint(CheckpointId("ckpt_forged"), NamespaceId("ns_a")) is None


def test_storage_rejects_cross_namespace_parent() -> None:
    storage, runtime, model = _build()
    with storage:
        a = _run(runtime, model, "ns_a", "ra")
        b = _run(runtime, model, "ns_b", "rb")
        a_ckpt = storage.get_latest_checkpoint(a.run_id, NamespaceId("ns_a"))
        assert a_ckpt is not None

        with pytest.raises(CrossNamespaceAccessError):
            storage.save_checkpoint(
                checkpoint_id=CheckpointId("ckpt_forged_ns"),
                run_id=b.run_id,
                step_id=b.step_history[0].step_id,
                branch_id=a_ckpt.branch_id,
                namespace_id=NamespaceId("ns_b"),
                state_payload={"k": "v"},
                created_at=datetime(2026, 10, 15, tzinfo=UTC),
                parent_checkpoint_id=a_ckpt.checkpoint_id,
            )


def test_storage_rejects_missing_and_self_parent() -> None:
    storage, runtime, model = _build()
    with storage:
        a = _run(runtime, model, "ns_a", "ra")
        step_id = a.step_history[0].step_id
        common: dict[str, Any] = {
            "run_id": a.run_id,
            "step_id": step_id,
            "branch_id": _BRANCH,
            "namespace_id": NamespaceId("ns_a"),
            "state_payload": {"k": "v"},
            "created_at": datetime(2026, 10, 15, tzinfo=UTC),
        }
        with pytest.raises(RecordNotFoundError):
            storage.save_checkpoint(
                checkpoint_id=CheckpointId("ckpt_x"),
                parent_checkpoint_id=CheckpointId("does_not_exist"),
                **common,
            )
        with pytest.raises(ValueError, match="own parent"):
            storage.save_checkpoint(
                checkpoint_id=CheckpointId("ckpt_self"),
                parent_checkpoint_id=CheckpointId("ckpt_self"),
                **common,
            )


def test_restore_seeds_run_scoped_lineage_head() -> None:
    storage, runtime, model = _build()
    with storage:
        res = _run(runtime, model, "ns_a", "ra")
        ckpt = storage.get_latest_checkpoint(res.run_id, NamespaceId("ns_a"))
        assert ckpt is not None
        state, _ = runtime.restore_from_checkpoint(ckpt.checkpoint_id, NamespaceId("ns_a"))
        assert state.run_id == RunId(str(res.run_id))
        assert state.last_checkpoint_id == ckpt.checkpoint_id

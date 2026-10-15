"""Pre-persistence secret boundary tests across every durable table (P2-H2).

All credentials below are synthetic and assembled from fragments so that this file itself does not
contain contiguous credential-shaped strings. These tests demonstrate that the *known-pattern*
boundary holds on every write path; they do not demonstrate comprehensive secret detection or any
physical-erasure guarantee.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memory_aware_agent_harness.contracts import (
    BranchId,
    CheckpointId,
    EventId,
    FailureDetails,
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    PrePersistenceSecurityError,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepCommitError,
    StepId,
    StepResult,
    TokenUsage,
    ToolCallRequest,
    ToolCallStatus,
    ToolInvocationId,
    ToolProtocol,
    ToolResult,
)
from memory_aware_agent_harness.doubles import DeterministicClock
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime
from memory_aware_agent_harness.storage import security
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter
from memory_aware_agent_harness.storage.security import PrePersistenceValidator

# --- Synthetic credentials (not real; fragments joined at runtime) -------------------------
SK = "sk-" + "test" + "SyntheticKeyAbCdEfGh12345678"
GH = "ghp_" + "SyntheticGithubTokenAbCdEfGhIjKlMnOp12"
AWS = "AKIA" + "SYNTHETIC1234567"
BEARER_VALUE = "SyntheticBearerValue" + "AbCdEf1234567890"
PRIVATE_BODY = "SYNTHETICPRIVATEKEYBODY" + "ZZZZ1234"
PRIVATE_KEY = f"-----BEGIN RSA PRIVATE KEY-----\n{PRIVATE_BODY}\n-----END RSA PRIVATE KEY-----"
BEARER = f"Bearer {BEARER_VALUE}"
NEEDLES = (SK, GH, AWS, BEARER_VALUE, PRIVATE_BODY)

NS = NamespaceId("ns_secrets")
NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)


def _scan_tables(conn: sqlite3.Connection) -> list[str]:
    hits: list[str] = []
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
        )
    ]
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table};"):
            for cell in row:
                text = cell.decode("utf-8", "ignore") if isinstance(cell, bytes) else str(cell)
                for needle in NEEDLES:
                    if needle in text:
                        hits.append(f"{table}:{needle[:8]}")
    return hits


def _scan_files(db_path: Path) -> list[str]:
    hits: list[str] = []
    for suffix in ("", "-wal"):
        path = Path(str(db_path) + suffix)
        if not path.exists():
            continue
        data = path.read_bytes()
        for needle in NEEDLES:
            if needle.encode() in data:
                hits.append(f"{path.name}:{needle[:8]}")
    return hits


def _mem(mem_id: str, **overrides: Any) -> MemoryRecord:
    base: dict[str, Any] = {
        "id": MemoryId(mem_id),
        "namespace_id": NS,
        "content": "benign",
        "kind": MemoryKind.SEMANTIC,
        "state": MemoryState.ACTIVE,
        "confidence": 0.9,
        "salience": 0.8,
        "origin": MemoryOrigin.AGENT_OBSERVATION,
        "metadata": {},
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return MemoryRecord(**base)


def _run(run_id: str, status: RunStatus | None = None, **kw: Any) -> RunResult:
    if status is None:
        status = (
            RunStatus.FAILED
            if kw.get("error") is not None or kw.get("failure_details") is not None
            else RunStatus.COMPLETED
        )
    return RunResult(
        run_id=RunId(run_id),
        namespace_id=NS,
        status=status,
        step_count=1,
        total_tokens_consumed=3,
        final_output=kw.pop("final_output", None),
        trace_id=f"trace_{run_id}",
        **kw,
    )


def _step(step_id: str, run_id: str, **kw: Any) -> StepResult:
    sid = StepId(step_id)
    return StepResult(
        step_id=sid,
        run_id=RunId(run_id),
        step_number=1,
        decision=ModelDecision(
            decision_id=f"dec_{step_id}",
            step_id=sid,
            finish_reason=ModelFinishReason.STOP,
            content=kw.pop("content", "ok"),
            raw_response=kw.pop("raw_response", None),
        ),
        tool_results=(),
        memories_proposed=0,
        memories_committed=0,
        tokens_consumed=3,
        completed_at=NOW,
        **kw,
    )


# =============================================================================================
# Validator unit behaviour
# =============================================================================================


def test_sanitize_payload_reaches_dicts_nested_in_lists() -> None:
    v = PrePersistenceValidator()
    out = v.sanitize_payload(
        {"a": [{"b": [{"c": f"x {SK}"}]}], "k": (GH,)}, namespace_id="n", field_name="f"
    )
    assert SK not in repr(out)
    assert GH not in repr(out)


def test_sanitize_payload_scrubs_secret_bearing_keys() -> None:
    v = PrePersistenceValidator()
    out = v.sanitize_payload({f"k_{SK}": 1}, namespace_id="n", field_name="f")
    assert SK not in repr(out)


def test_credential_named_key_is_redacted_when_value_format_unknown() -> None:
    v = PrePersistenceValidator()
    out = v.sanitize_payload(
        {"password": "hunter2hunter2", "token": "short"}, namespace_id="n", field_name="f"
    )
    assert out["password"] == "[REDACTED_CREDENTIAL]"
    assert out["token"] == "short"  # below the length heuristic: left alone


def test_specific_pattern_placeholder_wins_over_key_heuristic() -> None:
    v = PrePersistenceValidator()
    out = v.sanitize_payload({"token": GH}, namespace_id="n", field_name="f")
    assert out["token"] == "[REDACTED_GITHUB_TOKEN]"


def test_fail_closed_rejects_credential_named_key() -> None:
    v = PrePersistenceValidator(fail_closed=True)
    with pytest.raises(PrePersistenceSecurityError) as ei:
        v.sanitize_payload({"api_key": "opaque-value-123"}, namespace_id="n", field_name="f")
    assert "opaque-value-123" not in str(ei.value)


@pytest.mark.parametrize(
    "bad",
    [object(), {1: "non-string-key"}, {"a": {1, 2}}],
)
def test_unsupported_payload_types_fail_closed(bad: Any) -> None:
    v = PrePersistenceValidator()
    with pytest.raises(PrePersistenceSecurityError):
        v.sanitize_payload(bad, namespace_id="n", field_name="f")


def test_excessive_nesting_fails_closed() -> None:
    deep: Any = "leaf"
    for _ in range(100):
        deep = {"n": deep}
    with pytest.raises(PrePersistenceSecurityError) as ei:
        PrePersistenceValidator().sanitize_payload(deep, namespace_id="n", field_name="f")
    assert ei.value.rule_name == "PAYLOAD_TOO_DEEP"


def test_sanitizer_internal_failure_fails_closed_without_leaking(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(text: str) -> Any:
        raise RuntimeError(f"scanner exploded on {text}")

    monkeypatch.setattr(security, "redact_secrets", boom)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PrePersistenceSecurityError) as ei:
        PrePersistenceValidator().sanitize_text(f"data {SK}", namespace_id="n", field_name="f")
    exc = ei.value
    assert exc.__cause__ is None
    assert exc.rule_name == "SANITIZATION_FAILURE"
    assert SK not in str(exc) and SK not in repr(exc) and SK not in repr(dict(exc.details))
    assert all(SK not in r.getMessage() for r in caplog.records)


def test_sanitize_json_text_is_byte_stable_when_clean() -> None:
    v = PrePersistenceValidator()
    text = '{"b": 1,  "a": [1, 2]}'
    assert v.sanitize_json_text(text, namespace_id="n", field_name="f") == text


# =============================================================================================
# Every table, via adapter APIs (normal + error + rollback paths), db and WAL bytes scanned
# =============================================================================================


def test_every_table_and_wal_bytes_are_free_of_known_secret_patterns(tmp_path: Path) -> None:
    db = tmp_path / "secrets.db"
    adapter = SQLiteStorageAdapter(db)
    run_id = "run_s1"

    adapter.save_run(
        _run(
            run_id,
            final_output=f"result {SK}",
            error=f"error {GH}",
            failure_details=FailureDetails(error_type="E", message=f"detail {AWS}"),
        )
    )
    adapter.save_step(
        _step(
            "step_s1",
            run_id,
            content=f"decision {SK}",
            raw_response={"nested": [{"deep": [{"k": GH}]}]},
            failure_details=FailureDetails(error_type="E", message=f"step failed {BEARER}"),
        ),
        NS,
    )

    inv1, inv2 = ToolInvocationId("inv_1"), ToolInvocationId("inv_2")
    for inv, key in ((inv1, "idem_1"), (inv2, "idem_2")):
        adapter.record_tool_intent(
            invocation_id=inv,
            step_id=StepId("step_s1"),
            namespace_id=NS,
            tool_name=f"tool_{key}",
            arguments={"outer": [{"inner": {"auth": BEARER, "pk": PRIVATE_KEY}}]},
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key=key,
            dispatched_at=NOW,
            run_id=RunId(run_id),
        )
    adapter.record_tool_result(
        ToolResult(
            invocation_id=inv1,
            tool_name="tool_idem_1",
            status=ToolCallStatus.COMPLETED,
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key="idem_1",
            result={"rows": [{"token_dump": SK, "cfg": {"k": [GH]}}]},
        ),
        NS,
    )
    adapter.mark_invocation_unknown(inv2, NS, error=f"crashed holding {SK}")

    adapter.put(
        _mem("m1", content=f"note {SK}", metadata={"nested": {"list": [{"k": GH}]}, "x": AWS})
    )
    adapter.tombstone(MemoryId("m1"), NS, reason=f"revoked because {SK}")
    adapter.record_memory_lineage(MemoryId("m1"), NS, f"source {GH}", f"rel {AWS}")

    adapter.save_checkpoint(
        checkpoint_id=CheckpointId("ckpt_1"),
        run_id=RunId(run_id),
        step_id=StepId("step_s1"),
        branch_id=BranchId("main"),
        namespace_id=NS,
        state_payload={"wm": [{"secret_note": SK}], "k": 1},
        created_at=NOW,
    )
    ckpt = adapter.get_checkpoint_record(CheckpointId("ckpt_1"), NS)
    assert ckpt is not None
    assert ckpt.verify_integrity(), "hash must describe the sanitized payload that was persisted"

    adapter.append_trace_event(
        event_id=EventId("evt_1"),
        run_id=RunId(run_id),
        namespace_id=NS,
        sequence_num=0,
        event_type="tool_failed",
        payload={"detail": [{"msg": f"{PRIVATE_KEY}"}], "k": BEARER},
        emitted_at=NOW,
        step_id=StepId("step_s1"),
    )

    adapter.update_run_status(
        RunId(run_id),
        NS,
        RunStatus.FAILED,
        error=f"final failure {SK}",
        failure_details=FailureDetails(error_type="E", message=f"why {GH}"),
    )

    # Rollback / recovery path: orphaned run reconciled with a secret-bearing reason.
    adapter.save_run(_run("run_orphan", status=RunStatus.RUNNING))
    adapter.reconcile_orphaned_run(RunId("run_orphan"), NS, reason=f"operator note {SK}")

    assert _scan_tables(adapter.conn) == []
    # Database and un-checkpointed WAL bytes, before and after a WAL checkpoint.
    assert _scan_files(db) == []
    adapter.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    assert _scan_files(db) == []
    adapter.close()


def test_scanner_detects_a_secret_that_bypasses_the_boundary(tmp_path: Path) -> None:
    """Negative control: proves the scan above would notice a leak."""
    db = tmp_path / "control.db"
    adapter = SQLiteStorageAdapter(db)
    adapter.save_run(_run("run_c"))
    adapter.conn.execute(
        "UPDATE runs SET metadata_json = ? WHERE run_id = 'run_c';", (f'{{"x": "{SK}"}}',)
    )
    adapter.conn.commit()
    assert _scan_tables(adapter.conn) != []
    assert _scan_files(db) != []
    adapter.close()


def test_redaction_never_logs_secret_bearing_payloads(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = SQLiteStorageAdapter(tmp_path / "log.db")
    adapter.save_run(_run("run_l", final_output=f"{SK} {GH} {AWS} {BEARER}"))
    adapter.put(_mem("ml", content=PRIVATE_KEY, metadata={"k": [{"v": SK}]}))
    text = "\n".join(r.getMessage() for r in caplog.records)
    for needle in NEEDLES:
        assert needle not in text
    adapter.close()


# =============================================================================================
# Fail-closed behaviour
# =============================================================================================


def test_fail_closed_adapter_rejects_every_secret_bearing_write(tmp_path: Path) -> None:
    db = tmp_path / "closed.db"
    adapter = SQLiteStorageAdapter(db, validator=PrePersistenceValidator(fail_closed=True))
    adapter.save_run(_run("run_f"))
    adapter.save_step(_step("step_f", "run_f"), NS)

    with pytest.raises(PrePersistenceSecurityError):
        adapter.save_run(_run("run_f2", final_output=SK))
    with pytest.raises(PrePersistenceSecurityError):
        adapter.save_step(_step("step_f2", "run_f", content=GH), NS)
    with pytest.raises(PrePersistenceSecurityError):
        adapter.record_tool_intent(
            invocation_id=ToolInvocationId("inv_f"),
            step_id=StepId("step_f"),
            namespace_id=NS,
            tool_name="t",
            arguments={"a": [{"b": SK}]},
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key="k",
            dispatched_at=NOW,
            run_id=RunId("run_f"),
        )
    with pytest.raises(PrePersistenceSecurityError):
        adapter.put(_mem("mf", metadata={"deep": [{"k": AWS}]}))
    with pytest.raises(PrePersistenceSecurityError):
        adapter.save_checkpoint(
            checkpoint_id=CheckpointId("ckpt_f"),
            run_id=RunId("run_f"),
            step_id=StepId("step_f"),
            branch_id=BranchId("main"),
            namespace_id=NS,
            state_payload={"k": [BEARER]},
            created_at=NOW,
        )
    with pytest.raises(PrePersistenceSecurityError):
        adapter.append_trace_event(
            event_id=EventId("evt_f"),
            run_id=RunId("run_f"),
            namespace_id=NS,
            sequence_num=0,
            event_type="e",
            payload={"k": [{"v": PRIVATE_KEY}]},
            emitted_at=NOW,
        )

    for table, expected in (
        ("runs", 1),
        ("run_steps", 1),
        ("tool_invocations", 0),
        ("memory_records", 0),
        ("checkpoints", 0),
        ("trace_events", 0),
    ):
        count = adapter.conn.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]
        assert count == expected, table
    assert _scan_tables(adapter.conn) == []
    assert _scan_files(db) == []
    adapter.close()


def test_fail_closed_step_commit_rolls_back_atomically_without_leaking(tmp_path: Path) -> None:
    db = tmp_path / "commit.db"
    adapter = SQLiteStorageAdapter(db, validator=PrePersistenceValidator(fail_closed=True))
    with pytest.raises(StepCommitError) as ei:
        adapter.commit_step_transaction(
            step_result=_step("step_c", "run_c"),
            run_result=_run("run_c"),
            namespace_id=NS,
            proposed_memories=(_mem("mc", content=f"learned {SK}"),),
        )
    assert SK not in str(ei.value) and SK not in repr(dict(ei.value.details))
    assert adapter.get_run(RunId("run_c"), NS) is None
    assert _scan_tables(adapter.conn) == []
    assert _scan_files(db) == []
    adapter.close()


# =============================================================================================
# End to end: model output, tool arguments/results and exception messages through the runtime
# =============================================================================================


class _LeakyWrite(ToolProtocol):
    name = "leaky_write"
    description = "raises with a credential in the exception message"
    side_effect = SideEffectType.NON_IDEMPOTENT_WRITE

    def get_schema(self) -> Mapping[str, Any]:
        return {}

    def execute(self, arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
        raise RuntimeError(f"upstream rejected credential {SK}")


class _LeakyRead(ToolProtocol):
    name = "leaky_read"
    description = "returns nested credentials"
    side_effect = SideEffectType.READ_ONLY

    def get_schema(self) -> Mapping[str, Any]:
        return {}

    def execute(self, arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
        return {"rows": [{"auth": BEARER, "extra": {"pk": [PRIVATE_KEY]}}]}


def test_runtime_end_to_end_persists_no_known_secret_patterns(tmp_path: Path) -> None:
    db = tmp_path / "e2e.db"
    clock = DeterministicClock(NOW)
    usage = TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    model = FakeModelAdapter(
        responses=[
            ModelDecision(
                decision_id="d1",
                step_id=StepId("s1"),
                finish_reason=ModelFinishReason.TOOL_CALL,
                content=f"calling with {AWS}",
                tool_calls=(
                    ToolCallRequest(
                        call_id="c1",
                        tool_name="leaky_write",
                        arguments={"payload": {"items": [{"note": f"use {GH}"}]}, "api_key": SK},
                    ),
                    ToolCallRequest(call_id="c2", tool_name="leaky_read", arguments={}),
                ),
                token_usage=usage,
            ),
            ModelDecision(
                decision_id="d2",
                step_id=StepId("s2"),
                finish_reason=ModelFinishReason.STOP,
                content=f"finished; do not store {SK}",
                token_usage=usage,
            ),
        ]
    )
    adapter = SQLiteStorageAdapter(db, clock=clock.now)
    runtime = AgentRuntime(
        model=model, tools=[_LeakyWrite(), _LeakyRead()], storage=adapter, clock=clock
    )
    runtime.run("exercise the boundary", namespace_id=NS)

    assert _scan_tables(adapter.conn) == []
    assert _scan_files(db) == []
    adapter.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    assert _scan_files(db) == []
    adapter.close()

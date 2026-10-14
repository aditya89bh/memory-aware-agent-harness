"""Unit tests for SQLite tool invocations repository with namespace scoping."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from memory_aware_agent_harness.contracts import (
    CrossNamespaceAccessError,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    RunResult,
    RunStatus,
    SideEffectType,
    StepId,
    StepResult,
    TokenUsage,
    ToolCallStatus,
    ToolInvocationId,
    ToolResult,
)
from memory_aware_agent_harness.storage.connection import create_connection
from memory_aware_agent_harness.storage.migrations import apply_migrations
from memory_aware_agent_harness.storage.runs_repo import save_run, save_step
from memory_aware_agent_harness.storage.tools_repo import (
    find_invocation_by_idempotency_key,
    find_pending_or_unknown_invocations,
    get_tool_invocation,
    list_invocations_for_step,
    mark_invocation_unknown,
    record_tool_intent,
    record_tool_result,
)


def _setup_run_and_step(
    conn: sqlite3.Connection,
    run_id: str = "run_01",
    step_id: str = "step_01",
    namespace_id: str = "ns_alpha",
) -> tuple[RunId, StepId, NamespaceId]:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    run = RunResult(
        run_id=RunId(run_id),
        namespace_id=NamespaceId(namespace_id),
        status=RunStatus.RUNNING,
        step_count=1,
        total_tokens_consumed=100,
        final_output=None,
        trace_id="tr_01",
        token_usage=TokenUsage(prompt_tokens=80, completion_tokens=20, total_tokens=100),
        step_history=(),
        tool_results=(),
    )
    save_run(conn, run)

    step = StepResult(
        step_id=StepId(step_id),
        run_id=RunId(run_id),
        step_number=1,
        decision=ModelDecision(
            decision_id=f"dec_{step_id}",
            step_id=StepId(step_id),
            finish_reason=ModelFinishReason.TOOL_CALL,
        ),
        tool_results=(),
        memories_proposed=0,
        memories_committed=0,
        tokens_consumed=100,
        completed_at=now,
    )
    save_step(conn, step, NamespaceId(namespace_id))
    return RunId(run_id), StepId(step_id), NamespaceId(namespace_id)


def test_record_tool_intent_and_get_roundtrip() -> None:
    """record_tool_intent stores durable intent and get_tool_invocation fetches it cleanly."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_id, step_id, ns = _setup_run_and_step(conn)

        inv_id = ToolInvocationId("inv_01")
        record_tool_intent(
            conn,
            invocation_id=inv_id,
            step_id=step_id,
            run_id=run_id,
            namespace_id=ns,
            tool_name="stripe_charge",
            idempotency_key="tx_idem_123",
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            arguments={"amount": 5000, "currency": "USD"},
        )

        record = get_tool_invocation(conn, inv_id, ns)
        assert record is not None
        assert record.invocation_id == inv_id
        assert record.step_id == step_id
        assert record.run_id == run_id
        assert record.namespace_id == ns
        assert record.tool_name == "stripe_charge"
        assert record.idempotency_key == "tx_idem_123"
        assert record.side_effect == SideEffectType.NON_IDEMPOTENT_WRITE
        assert record.status == ToolCallStatus.PENDING
        assert json.loads(record.arguments_json) == {"amount": 5000, "currency": "USD"}
        assert record.result_json is None
        assert record.error_text is None
        assert record.completed_at is None

        # Re-recording same intent updates arguments without error
        record_tool_intent(
            conn,
            invocation_id=inv_id,
            step_id=step_id,
            run_id=run_id,
            namespace_id=ns,
            tool_name="stripe_charge",
            idempotency_key="tx_idem_123",
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            arguments={"amount": 6000, "currency": "USD"},
        )
        updated = get_tool_invocation(conn, inv_id, ns)
        assert updated is not None
        assert json.loads(updated.arguments_json) == {"amount": 6000, "currency": "USD"}

        # Nonexistent invocation returns None
        assert get_tool_invocation(conn, ToolInvocationId("missing"), ns) is None
    finally:
        conn.close()


def test_record_tool_result_and_idempotency_lookup() -> None:
    """record_tool_result commits completion and idempotency lookup finds it."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_id, step_id, ns = _setup_run_and_step(conn)

        inv_id = ToolInvocationId("inv_01")
        record_tool_intent(
            conn,
            invocation_id=inv_id,
            step_id=step_id,
            run_id=run_id,
            namespace_id=ns,
            tool_name="stripe_charge",
            idempotency_key="tx_idem_123",
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            arguments={"amount": 5000},
        )

        completed_dt = datetime(2026, 10, 14, 12, 1, 0, tzinfo=UTC)
        tool_res = ToolResult(
            invocation_id=inv_id,
            tool_name="stripe_charge",
            status=ToolCallStatus.COMPLETED,
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
            idempotency_key="tx_idem_123",
            result={"charge_id": "ch_999", "status": "succeeded"},
        )
        record_tool_result(
            conn,
            invocation_id=inv_id,
            namespace_id=ns,
            tool_result=tool_res,
            completed_at=completed_dt,
        )

        rec = get_tool_invocation(conn, inv_id, ns)
        assert rec is not None
        assert rec.status == ToolCallStatus.COMPLETED
        assert rec.result_json is not None
        assert json.loads(rec.result_json) == {"charge_id": "ch_999", "status": "succeeded"}
        assert rec.completed_at == completed_dt

        # Idempotency key lookup
        found = find_invocation_by_idempotency_key(conn, ns, "stripe_charge", "tx_idem_123")
        assert found is not None
        assert found.invocation_id == inv_id

        # Missing idempotency key returns None
        assert (
            find_invocation_by_idempotency_key(conn, ns, "stripe_charge", "unknown_key")
            is None
        )
    finally:
        conn.close()


def test_mark_invocation_unknown_and_pending_recovery() -> None:
    """mark_invocation_unknown flags uncertain outcomes and pending finder retrieves them."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_id, step_id, ns = _setup_run_and_step(conn)

        inv_pending = ToolInvocationId("inv_pend")
        inv_unknown = ToolInvocationId("inv_unk")
        inv_completed = ToolInvocationId("inv_comp")

        record_tool_intent(
            conn,
            invocation_id=inv_pending,
            step_id=step_id,
            run_id=run_id,
            namespace_id=ns,
            tool_name="search",
            idempotency_key="k1",
            side_effect=SideEffectType.READ_ONLY,
        )

        record_tool_intent(
            conn,
            invocation_id=inv_unknown,
            step_id=step_id,
            run_id=run_id,
            namespace_id=ns,
            tool_name="transfer",
            idempotency_key="k2",
            side_effect=SideEffectType.NON_IDEMPOTENT_WRITE,
        )
        mark_invocation_unknown(
            conn,
            inv_unknown,
            ns,
            error="Connection severed during wire transfer",
        )

        record_tool_intent(
            conn,
            invocation_id=inv_completed,
            step_id=step_id,
            run_id=run_id,
            namespace_id=ns,
            tool_name="status_check",
            idempotency_key="k3",
            side_effect=SideEffectType.READ_ONLY,
        )
        record_tool_result(
            conn,
            invocation_id=inv_completed,
            namespace_id=ns,
            tool_result=ToolResult(
                invocation_id=inv_completed,
                tool_name="status_check",
                status=ToolCallStatus.COMPLETED,
                side_effect=SideEffectType.READ_ONLY,
                idempotency_key="k3",
                result={"ok": True},
            ),
        )

        # find_pending_or_unknown_invocations returns both pending and unknown
        unresolved = find_pending_or_unknown_invocations(conn, ns, run_id=run_id)
        assert len(unresolved) == 2
        ids = {r.invocation_id for r in unresolved}
        assert ids == {inv_pending, inv_unknown}

        # Querying without run_id returns namespace wide unresolved
        all_unresolved = find_pending_or_unknown_invocations(conn, ns)
        assert len(all_unresolved) == 2

        # Step list contains all 3 invocations
        step_invs = list_invocations_for_step(conn, step_id, ns)
        assert len(step_invs) == 3
    finally:
        conn.close()


def test_tools_cross_namespace_access_denied() -> None:
    """All tool repository operations strictly enforce namespace isolation."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_alpha, step_alpha, ns_alpha = _setup_run_and_step(
            conn, run_id="r_a", step_id="s_a", namespace_id="ns_alpha"
        )
        run_beta, step_beta, ns_beta = _setup_run_and_step(
            conn, run_id="r_b", step_id="s_b", namespace_id="ns_beta"
        )

        inv_a = ToolInvocationId("inv_alpha")
        record_tool_intent(
            conn,
            invocation_id=inv_a,
            step_id=step_alpha,
            run_id=run_alpha,
            namespace_id=ns_alpha,
            tool_name="tool_a",
            idempotency_key="key_a",
            side_effect=SideEffectType.READ_ONLY,
        )

        # 1. Cross-namespace intent recording against wrong parent run
        with pytest.raises(CrossNamespaceAccessError):
            record_tool_intent(
                conn,
                invocation_id=ToolInvocationId("inv_forged_run"),
                step_id=step_beta,
                run_id=run_alpha,  # from ns_alpha!
                namespace_id=ns_beta,
                tool_name="tool_b",
                idempotency_key="key_b",
                side_effect=SideEffectType.READ_ONLY,
            )

        # 2. Cross-namespace intent recording against wrong parent step
        with pytest.raises(CrossNamespaceAccessError):
            record_tool_intent(
                conn,
                invocation_id=ToolInvocationId("inv_forged_step"),
                step_id=step_alpha,  # from ns_alpha!
                run_id=run_beta,
                namespace_id=ns_beta,
                tool_name="tool_b",
                idempotency_key="key_b",
                side_effect=SideEffectType.READ_ONLY,
            )

        # 3. Cross-namespace overwrite of existing invocation
        with pytest.raises(CrossNamespaceAccessError):
            record_tool_intent(
                conn,
                invocation_id=inv_a,  # belongs to ns_alpha!
                step_id=step_beta,
                run_id=run_beta,
                namespace_id=ns_beta,
                tool_name="tool_b",
                idempotency_key="key_b",
                side_effect=SideEffectType.READ_ONLY,
            )

        # 4. Cross-namespace get_tool_invocation
        with pytest.raises(CrossNamespaceAccessError):
            get_tool_invocation(conn, inv_a, ns_beta)

        # 5. Cross-namespace record_tool_result
        tr = ToolResult(
            invocation_id=inv_a,
            tool_name="tool_a",
            status=ToolCallStatus.COMPLETED,
            side_effect=SideEffectType.READ_ONLY,
            idempotency_key="key_a",
            result={},
        )
        with pytest.raises(CrossNamespaceAccessError):
            record_tool_result(conn, invocation_id=inv_a, namespace_id=ns_beta, tool_result=tr)

        # 6. Cross-namespace mark_invocation_unknown
        with pytest.raises(CrossNamespaceAccessError):
            mark_invocation_unknown(conn, inv_a, ns_beta)

        # 7. Cross-namespace list_invocations_for_step
        with pytest.raises(CrossNamespaceAccessError):
            list_invocations_for_step(conn, step_alpha, ns_beta)

        # 8. Cross-namespace find_pending_or_unknown_invocations with wrong run_id
        with pytest.raises(CrossNamespaceAccessError):
            find_pending_or_unknown_invocations(conn, ns_beta, run_id=run_alpha)

        # 9. Nonexistent resources raise RecordNotFoundError
        with pytest.raises(RecordNotFoundError):
            record_tool_intent(
                conn,
                invocation_id=ToolInvocationId("nonexistent"),
                step_id=StepId("missing_step"),
                run_id=run_alpha,
                namespace_id=ns_alpha,
                tool_name="tool_a",
                idempotency_key="k",
                side_effect=SideEffectType.READ_ONLY,
            )

        with pytest.raises(RecordNotFoundError):
            record_tool_intent(
                conn,
                invocation_id=ToolInvocationId("nonexistent"),
                step_id=step_alpha,
                run_id=RunId("missing_run"),
                namespace_id=ns_alpha,
                tool_name="tool_a",
                idempotency_key="k",
                side_effect=SideEffectType.READ_ONLY,
            )

        with pytest.raises(RecordNotFoundError):
            record_tool_result(
                conn,
                invocation_id=ToolInvocationId("missing_inv"),
                namespace_id=ns_alpha,
                tool_result=tr,
            )

        with pytest.raises(RecordNotFoundError):
            mark_invocation_unknown(conn, ToolInvocationId("missing_inv"), ns_alpha)

        # Mismatched step and run raises ValueError
        run_alpha2, _, _ = _setup_run_and_step(
            conn, run_id="r_a2", step_id="s_a2", namespace_id="ns_alpha"
        )
        with pytest.raises(ValueError, match="expected run"):
            record_tool_intent(
                conn,
                invocation_id=ToolInvocationId("inv_mismatch"),
                step_id=step_alpha,
                run_id=run_alpha2,
                namespace_id=ns_alpha,
                tool_name="tool_a",
                idempotency_key="k_mis",
                side_effect=SideEffectType.READ_ONLY,
            )

        # Querying step list for missing step returns empty tuple
        assert list_invocations_for_step(conn, StepId("missing_step"), ns_alpha) == ()

        # Querying pending for missing run returns empty tuple
        assert (
            find_pending_or_unknown_invocations(conn, ns_alpha, run_id=RunId("missing_run"))
            == ()
        )
    finally:
        conn.close()

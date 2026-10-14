"""Unit tests for SQLite trace events repository with sequence ordering and namespace scoping."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from memory_aware_agent_harness.contracts import (
    CrossNamespaceAccessError,
    EventId,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RecordNotFoundError,
    ReplayDivergenceError,
    RunId,
    RunResult,
    RunStatus,
    StepId,
    StepResult,
    TokenUsage,
    compute_state_hash,
)
from memory_aware_agent_harness.storage.connection import create_connection
from memory_aware_agent_harness.storage.migrations import apply_migrations
from memory_aware_agent_harness.storage.runs_repo import save_run, save_step
from memory_aware_agent_harness.storage.trace_repo import (
    append_trace_event,
    get_trace_event,
    get_trace_events,
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
            finish_reason=ModelFinishReason.STOP,
        ),
        tool_results=(),
        memories_proposed=0,
        memories_committed=0,
        tokens_consumed=100,
        completed_at=now,
    )
    save_step(conn, step, NamespaceId(namespace_id))
    return RunId(run_id), StepId(step_id), NamespaceId(namespace_id)


def test_append_and_get_trace_event_roundtrip() -> None:
    """append_trace_event saves immutable audit event and get_trace_event fetches it cleanly."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_id, step_id, ns = _setup_run_and_step(conn)

        ev_id = EventId("ev_01")
        payload = {"action": "dispatch_tool", "tool": "search"}
        t1 = datetime(2026, 10, 14, 12, 1, 0, tzinfo=UTC)

        saved = append_trace_event(
            conn,
            event_id=ev_id,
            run_id=run_id,
            namespace_id=ns,
            sequence_num=1,
            event_type="TOOL_DISPATCHED",
            payload=payload,
            emitted_at=t1,
            step_id=step_id,
        )

        assert saved.event_id == ev_id
        assert saved.run_id == run_id
        assert saved.step_id == step_id
        assert saved.namespace_id == ns
        assert saved.sequence_num == 1
        assert saved.event_type == "TOOL_DISPATCHED"
        assert saved.payload() == payload
        assert saved.state_hash == compute_state_hash(payload)

        fetched = get_trace_event(conn, ev_id, ns, verify_hash=True)
        assert fetched is not None
        assert fetched.event_id == ev_id
        assert fetched.payload() == payload
        assert fetched.state_hash == saved.state_hash

        # Nonexistent event returns None
        assert get_trace_event(conn, EventId("missing"), ns) is None
    finally:
        conn.close()


def test_trace_event_tamper_detection() -> None:
    """Tampering with trace payload raises ReplayDivergenceError upon retrieval."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_id, _, ns = _setup_run_and_step(conn)

        ev_id = EventId("ev_tamper")
        append_trace_event(
            conn,
            event_id=ev_id,
            run_id=run_id,
            namespace_id=ns,
            sequence_num=1,
            event_type="STATE_UPDATE",
            payload={"authorized": True},
        )

        # Directly tamper with payload in database
        conn.execute(
            "UPDATE trace_events SET payload_json = ? WHERE event_id = ?;",
            ('{"authorized":false}', str(ev_id)),
        )

        with pytest.raises(ReplayDivergenceError):
            get_trace_event(conn, ev_id, ns, verify_hash=True)

        # verify_hash=False succeeds without raising
        record = get_trace_event(conn, ev_id, ns, verify_hash=False)
        assert record is not None
        assert record.payload() == {"authorized": False}
    finally:
        conn.close()


def test_trace_events_sequence_ordering_and_pagination() -> None:
    """get_trace_events retrieves events ordered by sequence_num with pagination."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_id, step_id, ns = _setup_run_and_step(conn)

        # Append events out of order to verify ordered retrieval
        for seq, ev_type in [(3, "STEP_COMPLETED"), (1, "STEP_STARTED"), (2, "TOOL_CALLED")]:
            append_trace_event(
                conn,
                event_id=EventId(f"ev_{seq}"),
                run_id=run_id,
                namespace_id=ns,
                sequence_num=seq,
                event_type=ev_type,
                payload={"seq": seq},
                step_id=step_id,
            )

        events = get_trace_events(conn, run_id, ns, verify_hash=True)
        assert len(events) == 3
        assert [e.sequence_num for e in events] == [1, 2, 3]
        assert [e.event_type for e in events] == [
            "STEP_STARTED",
            "TOOL_CALLED",
            "STEP_COMPLETED",
        ]

        # Test from_sequence pagination
        paged = get_trace_events(conn, run_id, ns, from_sequence=2)
        assert len(paged) == 2
        assert [e.sequence_num for e in paged] == [2, 3]

        # Test limit pagination
        limited = get_trace_events(conn, run_id, ns, from_sequence=1, limit=2)
        assert len(limited) == 2
        assert [e.sequence_num for e in limited] == [1, 2]
    finally:
        conn.close()


def test_trace_events_serializable_record_and_update() -> None:
    """append_trace_event supports SerializableRecord and in-place updates."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_id, step_id, ns = _setup_run_and_step(conn)

        tu = TokenUsage(prompt_tokens=40, completion_tokens=60, total_tokens=100)
        ev = append_trace_event(
            conn,
            event_id=EventId("ev_sr"),
            run_id=run_id,
            namespace_id=ns,
            sequence_num=1,
            event_type="TOKEN_ACCOUNTING",
            payload=tu,
            step_id=step_id,
        )
        assert ev.state_hash == compute_state_hash(tu)
        assert ev.payload() == tu.to_dict()

        # Update event
        updated = append_trace_event(
            conn,
            event_id=EventId("ev_sr"),
            run_id=run_id,
            namespace_id=ns,
            sequence_num=1,
            event_type="TOKEN_ACCOUNTING_CORRECTED",
            payload={"corrected": True},
            step_id=step_id,
        )
        assert updated.event_type == "TOKEN_ACCOUNTING_CORRECTED"
        assert updated.payload() == {"corrected": True}
    finally:
        conn.close()


def test_trace_cross_namespace_access_denied() -> None:
    """Trace event operations strictly enforce namespace scoping and isolation."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_a, step_a, ns_a = _setup_run_and_step(
            conn, run_id="r_a", step_id="s_a", namespace_id="ns_alpha"
        )
        run_b, step_b, ns_b = _setup_run_and_step(
            conn, run_id="r_b", step_id="s_b", namespace_id="ns_beta"
        )

        ev_a = append_trace_event(
            conn,
            event_id=EventId("ev_alpha"),
            run_id=run_a,
            namespace_id=ns_a,
            sequence_num=1,
            event_type="RUN_STARTED",
            payload={},
        )

        # 1. Cross-namespace append with run from another namespace
        with pytest.raises(CrossNamespaceAccessError):
            append_trace_event(
                conn,
                event_id=EventId("ev_forged_run"),
                run_id=run_a,  # belongs to ns_alpha!
                namespace_id=ns_b,
                sequence_num=1,
                event_type="ATTACK",
                payload={},
            )

        # 2. Cross-namespace append with step from another namespace
        with pytest.raises(CrossNamespaceAccessError):
            append_trace_event(
                conn,
                event_id=EventId("ev_forged_step"),
                run_id=run_b,
                namespace_id=ns_b,
                sequence_num=1,
                event_type="ATTACK",
                payload={},
                step_id=step_a,  # belongs to ns_alpha!
            )

        # 3. Cross-namespace overwrite of existing event
        with pytest.raises(CrossNamespaceAccessError):
            append_trace_event(
                conn,
                event_id=ev_a.event_id,  # belongs to ns_alpha!
                run_id=run_b,
                namespace_id=ns_b,
                sequence_num=1,
                event_type="ATTACK",
                payload={},
            )

        # 4. Cross-namespace get_trace_event
        with pytest.raises(CrossNamespaceAccessError):
            get_trace_event(conn, ev_a.event_id, ns_b)

        # 5. Cross-namespace get_trace_events
        with pytest.raises(CrossNamespaceAccessError):
            get_trace_events(conn, run_a, ns_b)

        # 6. Nonexistent resources raise RecordNotFoundError
        with pytest.raises(RecordNotFoundError):
            append_trace_event(
                conn,
                event_id=EventId("ev_missing_r"),
                run_id=RunId("missing_run"),
                namespace_id=ns_a,
                sequence_num=1,
                event_type="INFO",
                payload={},
            )

        with pytest.raises(RecordNotFoundError):
            append_trace_event(
                conn,
                event_id=EventId("ev_missing_s"),
                run_id=run_a,
                namespace_id=ns_a,
                sequence_num=1,
                event_type="INFO",
                payload={},
                step_id=StepId("missing_step"),
            )

        # 7. Step not matching run raises ValueError
        run_a2, _, _ = _setup_run_and_step(
            conn, run_id="r_a2", step_id="s_a2", namespace_id="ns_alpha"
        )
        with pytest.raises(ValueError, match="expected run"):
            append_trace_event(
                conn,
                event_id=EventId("ev_mis"),
                run_id=run_a2,
                namespace_id=ns_a,
                sequence_num=1,
                event_type="INFO",
                payload={},
                step_id=step_a,
            )

        # 8. Negative sequence_num raises ValueError
        with pytest.raises(ValueError, match="sequence_num must be non-negative"):
            append_trace_event(
                conn,
                event_id=EventId("ev_neg"),
                run_id=run_a,
                namespace_id=ns_a,
                sequence_num=-1,
                event_type="INFO",
                payload={},
            )

        # Querying events for missing run returns empty tuple
        assert get_trace_events(conn, RunId("missing_run"), ns_a) == ()
    finally:
        conn.close()

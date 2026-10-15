"""Unit tests for SQLite checkpoints repository with state hash integrity and namespace scoping."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from memory_aware_agent_harness.contracts import (
    BranchId,
    CheckpointId,
    CrossNamespaceAccessError,
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
from memory_aware_agent_harness.storage.checkpoints_repo import (
    get_checkpoint,
    get_latest_checkpoint,
    list_checkpoints_for_run,
    save_checkpoint,
)
from memory_aware_agent_harness.storage.connection import create_connection
from memory_aware_agent_harness.storage.migrations import apply_migrations
from memory_aware_agent_harness.storage.runs_repo import save_run, save_step


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


def test_save_and_get_checkpoint_roundtrip() -> None:
    """save_checkpoint computes state_hash and get_checkpoint verifies integrity cleanly."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_id, step_id, ns = _setup_run_and_step(conn)

        ckpt_id = CheckpointId("ckpt_01")
        payload = {"variables": {"count": 42}, "status": "active"}

        created_dt = datetime(2026, 10, 14, 12, 1, 0, tzinfo=UTC)
        saved = save_checkpoint(
            conn,
            checkpoint_id=ckpt_id,
            run_id=run_id,
            step_id=step_id,
            branch_id=BranchId("main"),
            namespace_id=ns,
            state_payload=payload,
            created_at=created_dt,
        )

        assert saved.checkpoint_id == ckpt_id
        assert saved.run_id == run_id
        assert saved.step_id == step_id
        assert saved.branch_id == BranchId("main")
        assert saved.namespace_id == ns
        assert saved.parent_checkpoint_id is None
        assert saved.state_payload() == payload
        assert len(saved.state_hash) == 64  # SHA-256 hex digest

        fetched = get_checkpoint(conn, ckpt_id, ns, verify_hash=True)
        assert fetched is not None
        assert fetched.checkpoint_id == ckpt_id
        assert fetched.state_hash == saved.state_hash
        assert fetched.state_payload() == payload

        # Saving with a SerializableRecord payload
        tu = TokenUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100)
        saved_sr = save_checkpoint(
            conn,
            checkpoint_id=CheckpointId("ckpt_sr"),
            run_id=run_id,
            step_id=step_id,
            branch_id=BranchId("main"),
            namespace_id=ns,
            state_payload=tu,
        )
        fetched_sr = get_checkpoint(conn, CheckpointId("ckpt_sr"), ns, verify_hash=True)
        assert fetched_sr is not None
        assert fetched_sr.state_payload() == tu.to_dict()
        assert saved_sr.state_hash == compute_state_hash(tu)

        # Updating an existing checkpoint in same namespace
        updated = save_checkpoint(
            conn,
            checkpoint_id=ckpt_id,
            run_id=run_id,
            step_id=step_id,
            branch_id=BranchId("main"),
            namespace_id=ns,
            state_payload={"variables": {"count": 43}, "status": "active"},
        )
        assert updated.state_payload() == {"variables": {"count": 43}, "status": "active"}

        # Nonexistent checkpoint returns None
        assert get_checkpoint(conn, CheckpointId("missing"), ns) is None
    finally:
        conn.close()


def test_checkpoint_tamper_detection() -> None:
    """Tampering with state_payload_json triggers ReplayDivergenceError on retrieval."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_id, step_id, ns = _setup_run_and_step(conn)

        ckpt_id = CheckpointId("ckpt_secure")
        save_checkpoint(
            conn,
            checkpoint_id=ckpt_id,
            run_id=run_id,
            step_id=step_id,
            branch_id=BranchId("main"),
            namespace_id=ns,
            state_payload={"secret_value": 100},
        )

        # Directly tamper with the database row
        conn.execute(
            "UPDATE checkpoints SET state_payload_json = ? WHERE checkpoint_id = ?;",
            ('{"secret_value":9999}', str(ckpt_id)),
        )

        # get_checkpoint with verify_hash=True raises ReplayDivergenceError
        with pytest.raises(ReplayDivergenceError) as exc_info:
            get_checkpoint(conn, ckpt_id, ns, verify_hash=True)
        assert exc_info.value.expected_hash is not None
        assert exc_info.value.actual_hash is not None
        assert exc_info.value.expected_hash != exc_info.value.actual_hash

        # With verify_hash=False, retrieval succeeds without validation
        tampered = get_checkpoint(conn, ckpt_id, ns, verify_hash=False)
        assert tampered is not None
        assert tampered.state_payload() == {"secret_value": 9999}
    finally:
        conn.close()


def test_checkpoint_lineage_and_latest_lookup() -> None:
    """Parent checkpoints establish lineage and get_latest_checkpoint respects branches."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_id, step1_id, ns = _setup_run_and_step(conn, step_id="step_01")

        now = datetime(2026, 10, 14, 12, 10, 0, tzinfo=UTC)
        step2 = StepResult(
            step_id=StepId("step_02"),
            run_id=run_id,
            step_number=2,
            decision=ModelDecision(
                decision_id="dec_02",
                step_id=StepId("step_02"),
                finish_reason=ModelFinishReason.STOP,
            ),
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
            tokens_consumed=50,
            completed_at=now,
        )
        save_step(conn, step2, ns)

        # First checkpoint on main branch
        t1 = datetime(2026, 10, 14, 12, 5, 0, tzinfo=UTC)
        ckpt1 = save_checkpoint(
            conn,
            checkpoint_id=CheckpointId("ckpt_root"),
            run_id=run_id,
            step_id=step1_id,
            branch_id=BranchId("main"),
            namespace_id=ns,
            state_payload={"step": 1},
            created_at=t1,
        )

        # Second checkpoint extending root
        t2 = datetime(2026, 10, 14, 12, 11, 0, tzinfo=UTC)
        ckpt2 = save_checkpoint(
            conn,
            checkpoint_id=CheckpointId("ckpt_child"),
            run_id=run_id,
            step_id=step2.step_id,
            branch_id=BranchId("main"),
            namespace_id=ns,
            state_payload={"step": 2},
            parent_checkpoint_id=ckpt1.checkpoint_id,
            created_at=t2,
        )

        assert ckpt2.parent_checkpoint_id == ckpt1.checkpoint_id

        # Third checkpoint on experiment fork branch
        t3 = datetime(2026, 10, 14, 12, 12, 0, tzinfo=UTC)
        save_checkpoint(
            conn,
            checkpoint_id=CheckpointId("ckpt_fork"),
            run_id=run_id,
            step_id=step2.step_id,
            branch_id=BranchId("experiment"),
            namespace_id=ns,
            state_payload={"step": 2, "forked": True},
            parent_checkpoint_id=ckpt1.checkpoint_id,
            created_at=t3,
        )

        # Latest on main branch is ckpt_child
        latest_main = get_latest_checkpoint(conn, ns, run_id, BranchId("main"))
        assert latest_main is not None
        assert latest_main.checkpoint_id == CheckpointId("ckpt_child")

        # Latest on experiment branch is ckpt_fork
        latest_exp = get_latest_checkpoint(conn, ns, run_id, BranchId("experiment"))
        assert latest_exp is not None
        assert latest_exp.checkpoint_id == CheckpointId("ckpt_fork")

        # Missing branch returns None
        assert get_latest_checkpoint(conn, ns, run_id, BranchId("nonexistent")) is None

        # List all checkpoints for run
        all_ckpts = list_checkpoints_for_run(conn, run_id, ns)
        assert len(all_ckpts) == 3
        assert [c.checkpoint_id for c in all_ckpts] == [
            CheckpointId("ckpt_root"),
            CheckpointId("ckpt_child"),
            CheckpointId("ckpt_fork"),
        ]

        # List checkpoints filtered by branch
        main_ckpts = list_checkpoints_for_run(conn, run_id, ns, branch_id=BranchId("main"))
        assert len(main_ckpts) == 2
        assert [c.checkpoint_id for c in main_ckpts] == [
            CheckpointId("ckpt_root"),
            CheckpointId("ckpt_child"),
        ]
    finally:
        conn.close()


def test_checkpoints_cross_namespace_access_denied() -> None:
    """Checkpoint operations strictly enforce namespace scoping and tenant isolation."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        run_a, step_a, ns_a = _setup_run_and_step(
            conn, run_id="r_a", step_id="s_a", namespace_id="ns_alpha"
        )
        run_b, step_b, ns_b = _setup_run_and_step(
            conn, run_id="r_b", step_id="s_b", namespace_id="ns_beta"
        )

        ckpt_a = save_checkpoint(
            conn,
            checkpoint_id=CheckpointId("ckpt_a"),
            run_id=run_a,
            step_id=step_a,
            branch_id=BranchId("main"),
            namespace_id=ns_a,
            state_payload={"data": "alpha"},
        )

        # 1. Cross-namespace save with run from another namespace
        with pytest.raises(CrossNamespaceAccessError):
            save_checkpoint(
                conn,
                checkpoint_id=CheckpointId("ckpt_forged_run"),
                run_id=run_a,  # belongs to ns_alpha!
                step_id=step_b,
                branch_id=BranchId("main"),
                namespace_id=ns_b,
                state_payload={"data": "tamper"},
            )

        # 2. Cross-namespace save with step from another namespace
        with pytest.raises(CrossNamespaceAccessError):
            save_checkpoint(
                conn,
                checkpoint_id=CheckpointId("ckpt_forged_step"),
                run_id=run_b,
                step_id=step_a,  # belongs to ns_alpha!
                branch_id=BranchId("main"),
                namespace_id=ns_b,
                state_payload={"data": "tamper"},
            )

        # 3. Cross-namespace parent checkpoint reference
        with pytest.raises(CrossNamespaceAccessError):
            save_checkpoint(
                conn,
                checkpoint_id=CheckpointId("ckpt_forged_parent"),
                run_id=run_b,
                step_id=step_b,
                branch_id=BranchId("main"),
                namespace_id=ns_b,
                state_payload={"data": "tamper"},
                parent_checkpoint_id=ckpt_a.checkpoint_id,  # belongs to ns_alpha!
            )

        # 4. Cross-namespace overwrite attempt
        with pytest.raises(CrossNamespaceAccessError):
            save_checkpoint(
                conn,
                checkpoint_id=ckpt_a.checkpoint_id,  # belongs to ns_alpha!
                run_id=run_b,
                step_id=step_b,
                branch_id=BranchId("main"),
                namespace_id=ns_b,
                state_payload={"data": "tamper"},
            )

        # 5. Cross-namespace get_checkpoint returns None without disclosing existence
        assert get_checkpoint(conn, ckpt_a.checkpoint_id, ns_b) is None

        # 6. Cross-namespace get_latest_checkpoint returns None without disclosing existence
        assert get_latest_checkpoint(conn, ns_b, run_a, BranchId("main")) is None

        # 7. Cross-namespace list_checkpoints_for_run returns () without disclosing existence
        assert list_checkpoints_for_run(conn, run_a, ns_b) == ()

        # 8. Nonexistent resources raise RecordNotFoundError
        with pytest.raises(RecordNotFoundError):
            save_checkpoint(
                conn,
                checkpoint_id=CheckpointId("missing_r"),
                run_id=RunId("missing_run"),
                step_id=step_a,
                branch_id=BranchId("main"),
                namespace_id=ns_a,
                state_payload={},
            )

        with pytest.raises(RecordNotFoundError):
            save_checkpoint(
                conn,
                checkpoint_id=CheckpointId("missing_s"),
                run_id=run_a,
                step_id=StepId("missing_step"),
                branch_id=BranchId("main"),
                namespace_id=ns_a,
                state_payload={},
            )

        with pytest.raises(RecordNotFoundError):
            save_checkpoint(
                conn,
                checkpoint_id=CheckpointId("missing_p"),
                run_id=run_a,
                step_id=step_a,
                branch_id=BranchId("main"),
                namespace_id=ns_a,
                state_payload={},
                parent_checkpoint_id=CheckpointId("nonexistent_parent"),
            )

        # 9. Step not matching run raises ValueError
        run_a2, _, _ = _setup_run_and_step(
            conn, run_id="r_a2", step_id="s_a2", namespace_id="ns_alpha"
        )
        with pytest.raises(ValueError, match="expected run"):
            save_checkpoint(
                conn,
                checkpoint_id=CheckpointId("mismatch_step_run"),
                run_id=run_a2,
                step_id=step_a,  # step_a belongs to run_a, not run_a2
                branch_id=BranchId("main"),
                namespace_id=ns_a,
                state_payload={},
            )

        # Listing for missing run returns empty tuple
        assert list_checkpoints_for_run(conn, RunId("missing_run"), ns_a) == ()
        assert get_latest_checkpoint(conn, ns_a, RunId("missing_run")) is None
    finally:
        conn.close()

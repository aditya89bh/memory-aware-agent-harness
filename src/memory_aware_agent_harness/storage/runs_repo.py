"""SQLite repository for execution runs and discrete step outcomes with namespace scoping."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from memory_aware_agent_harness.contracts import (
    CrossNamespaceAccessError,
    FailureDetails,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    RunResult,
    RunStatus,
    StepId,
    StepResult,
    TokenUsage,
    ToolResult,
)
from memory_aware_agent_harness.storage.connection import transaction
from memory_aware_agent_harness.storage.security import (
    PrePersistenceValidator,
    resolve_validator,
)


def save_run(
    conn: sqlite3.Connection,
    run_result: RunResult,
    *,
    session_id: str = "default_session",
    max_steps: int = 50,
    token_budget: int = 100_000,
    created_at: datetime | None = None,
    completed_at: datetime | None = None,
    validator: PrePersistenceValidator | None = None,
) -> None:
    """Persist or update an execution run with tenant namespace enforcement.

    Args:
        conn: Target SQLite connection.
        run_result: RunResult contract to persist.
        session_id: Optional session identifier.
        max_steps: Maximum step budget.
        token_budget: Maximum token budget.
        created_at: Run creation timestamp.
        completed_at: Run completion timestamp.

    Raises:
        CrossNamespaceAccessError: If existing run belongs to a different namespace.
    """
    with transaction(conn):
        row = conn.execute(
            """
            SELECT namespace_id, session_id, max_steps, token_budget, created_at
            FROM runs WHERE run_id = ?;
            """,
            (str(run_result.run_id),),
        ).fetchone()

        if row is not None and row["namespace_id"] != str(run_result.namespace_id):
            raise CrossNamespaceAccessError(
                f"Cross-namespace access denied: run '{run_result.run_id}' belongs to "
                f"namespace '{row['namespace_id']}', cannot mutate under "
                f"namespace '{run_result.namespace_id}'",
                requested_namespace=str(run_result.namespace_id),
                target_namespace=str(row["namespace_id"]),
                resource_id=str(run_result.run_id),
            )

        eff_session_id = row["session_id"] if row else session_id
        eff_max_steps = int(row["max_steps"]) if row else max_steps
        eff_token_budget = int(row["token_budget"]) if row else token_budget
        now_iso = datetime.now(UTC).isoformat()
        eff_created_at = (
            row["created_at"]
            if row
            else (created_at.astimezone(UTC).isoformat() if created_at else now_iso)
        )
        eff_completed_at = completed_at.astimezone(UTC).isoformat() if completed_at else None

        metadata_dict = {
            "final_output": run_result.final_output,
            "trace_id": run_result.trace_id,
            "error": run_result.error,
            "token_usage": run_result.token_usage.to_dict() if run_result.token_usage else None,
            "failure_details": (
                run_result.failure_details.to_dict() if run_result.failure_details else None
            ),
            "tool_results": [tr.to_dict() for tr in run_result.tool_results],
        }
        metadata_json = json.dumps(metadata_dict, sort_keys=True, separators=(",", ":"))
        metadata_json = str(
            resolve_validator(validator).sanitize_json_text(
                metadata_json,
                namespace_id=str(run_result.namespace_id),
                field_name="runs.metadata_json",
                record_id=str(run_result.run_id),
            )
        )

        conn.execute(
            """
            INSERT INTO runs (
                run_id, session_id, namespace_id, status, step_count,
                max_steps, token_budget, tokens_consumed, created_at,
                completed_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                session_id = excluded.session_id,
                status = excluded.status,
                step_count = excluded.step_count,
                max_steps = excluded.max_steps,
                token_budget = excluded.token_budget,
                tokens_consumed = excluded.tokens_consumed,
                completed_at = excluded.completed_at,
                metadata_json = excluded.metadata_json;
            """,
            (
                str(run_result.run_id),
                str(eff_session_id),
                str(run_result.namespace_id),
                run_result.status.value,
                run_result.step_count,
                eff_max_steps,
                eff_token_budget,
                run_result.total_tokens_consumed,
                eff_created_at,
                eff_completed_at,
                metadata_json,
            ),
        )

        # Also persist all steps from step_history
        for step in run_result.step_history:
            save_step(conn, step, run_result.namespace_id, validator=validator)


def get_run(
    conn: sqlite3.Connection,
    run_id: RunId,
    namespace_id: NamespaceId,
) -> RunResult | None:
    """Retrieve an execution run by ID, validating tenant namespace scoping.

    Args:
        conn: Target SQLite connection.
        run_id: ID of the run to fetch.
        namespace_id: Expected tenant namespace.

    Returns:
        Reconstructed RunResult, or None if not found.

    Raises:
        CrossNamespaceAccessError: If the run belongs to a different namespace.
    """
    row = conn.execute(
        "SELECT * FROM runs WHERE run_id = ?;",
        (str(run_id),),
    ).fetchone()

    if row is None:
        return None

    if row["namespace_id"] != str(namespace_id):
        raise CrossNamespaceAccessError(
            f"Cross-namespace access denied: run '{run_id}' belongs to "
            f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
            requested_namespace=str(namespace_id),
            target_namespace=str(row["namespace_id"]),
            resource_id=str(run_id),
        )

    # Fetch steps
    steps = list_steps_for_run(conn, run_id, namespace_id)

    meta_raw = row["metadata_json"]
    meta = json.loads(meta_raw) if meta_raw else {}

    tu_raw = meta.get("token_usage")
    token_usage = TokenUsage.from_dict(tu_raw) if tu_raw else None

    fd_raw = meta.get("failure_details")
    failure_details = FailureDetails.from_dict(fd_raw) if fd_raw else None

    trs_raw = meta.get("tool_results", ())
    tool_results = tuple(ToolResult.from_dict(tr) for tr in trs_raw)

    return RunResult(
        run_id=RunId(str(row["run_id"])),
        namespace_id=NamespaceId(str(row["namespace_id"])),
        status=RunStatus(row["status"]),
        step_count=int(row["step_count"]),
        total_tokens_consumed=int(row["tokens_consumed"]),
        final_output=meta.get("final_output"),
        trace_id=str(meta.get("trace_id", "")),
        error=meta.get("error"),
        token_usage=token_usage,
        failure_details=failure_details,
        step_history=steps,
        tool_results=tool_results,
    )


def save_step(
    conn: sqlite3.Connection,
    step_result: StepResult,
    namespace_id: NamespaceId,
    *,
    state: str = "completed",
    context_manifest_id: str | None = None,
    started_at: datetime | None = None,
    validator: PrePersistenceValidator | None = None,
) -> None:
    """Persist a single step outcome with tenant namespace scoping.

    Args:
        conn: Target SQLite connection.
        step_result: StepResult contract to persist.
        namespace_id: Tenant namespace.
        state: Discrete execution state name.
        context_manifest_id: Optional manifest identifier.
        started_at: Optional start timestamp.

    Raises:
        RecordNotFoundError: If parent run does not exist.
        CrossNamespaceAccessError: If parent run or existing step belongs to another namespace.
    """
    with transaction(conn):
        run_row = conn.execute(
            "SELECT namespace_id FROM runs WHERE run_id = ?;",
            (str(step_result.run_id),),
        ).fetchone()

        if run_row is None:
            raise RecordNotFoundError(
                f"Parent run '{step_result.run_id}' not found for step '{step_result.step_id}'",
                record_id=str(step_result.run_id),
                namespace_id=str(namespace_id),
                table="runs",
            )

        if run_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Parent run '{step_result.run_id}' belongs to namespace "
                f"'{run_row['namespace_id']}', cannot write step under namespace '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(run_row["namespace_id"]),
                resource_id=str(step_result.run_id),
            )

        step_row = conn.execute(
            "SELECT namespace_id FROM run_steps WHERE step_id = ?;",
            (str(step_result.step_id),),
        ).fetchone()

        if step_row is not None and step_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Step '{step_result.step_id}' belongs to namespace '{step_row['namespace_id']}', "
                f"cannot mutate under namespace '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(step_row["namespace_id"]),
                resource_id=str(step_result.step_id),
            )

        model_decision_json = step_result.decision.to_json() if step_result.decision else None
        step_result_json = step_result.to_json()
        val = resolve_validator(validator)
        model_decision_json = val.sanitize_json_text(
            model_decision_json,
            namespace_id=str(namespace_id),
            field_name="run_steps.model_decision_json",
            record_id=str(step_result.step_id),
        )
        step_result_json = str(
            val.sanitize_json_text(
                step_result_json,
                namespace_id=str(namespace_id),
                field_name="run_steps.step_result_json",
                record_id=str(step_result.step_id),
            )
        )
        eff_started_at = (
            started_at.astimezone(UTC).isoformat()
            if started_at
            else step_result.completed_at.astimezone(UTC).isoformat()
        )

        conn.execute(
            """
            INSERT INTO run_steps (
                step_id, run_id, namespace_id, step_number, state,
                context_manifest_id, model_decision_json, started_at,
                completed_at, step_result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(step_id) DO UPDATE SET
                state = excluded.state,
                context_manifest_id = excluded.context_manifest_id,
                model_decision_json = excluded.model_decision_json,
                completed_at = excluded.completed_at,
                step_result_json = excluded.step_result_json;
            """,
            (
                str(step_result.step_id),
                str(step_result.run_id),
                str(namespace_id),
                step_result.step_number,
                state,
                context_manifest_id,
                model_decision_json,
                eff_started_at,
                step_result.completed_at.astimezone(UTC).isoformat(),
                step_result_json,
            ),
        )


def record_step_start(
    conn: sqlite3.Connection,
    *,
    step_id: StepId,
    run_id: RunId,
    namespace_id: NamespaceId,
    step_number: int,
    started_at: datetime | None = None,
) -> None:
    """Record initial step execution state before dispatching decisions or tools.

    Ensures the step exists in run_steps so subsequent tool invocations satisfy
    foreign key and namespace constraints.

    Args:
        conn: Target SQLite connection.
        step_id: Unique step identifier.
        run_id: Parent execution run.
        namespace_id: Tenant namespace.
        step_number: 1-indexed step sequence number.
        started_at: Step initiation timestamp.

    Raises:
        RecordNotFoundError: If parent run does not exist.
        CrossNamespaceAccessError: If parent run belongs to another namespace.
    """
    with transaction(conn):
        run_row = conn.execute(
            "SELECT namespace_id FROM runs WHERE run_id = ?;",
            (str(run_id),),
        ).fetchone()

        if run_row is None:
            raise RecordNotFoundError(
                f"Parent run '{run_id}' not found for step '{step_id}'",
                record_id=str(run_id),
                namespace_id=str(namespace_id),
                table="runs",
            )

        if run_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Parent run '{run_id}' belongs to namespace '{run_row['namespace_id']}', "
                f"cannot write step under namespace '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(run_row["namespace_id"]),
                resource_id=str(run_id),
            )

        eff_started_at = (
            started_at.astimezone(UTC).isoformat() if started_at else datetime.now(UTC).isoformat()
        )

        conn.execute(
            """
            INSERT INTO run_steps (
                step_id, run_id, namespace_id, step_number, state, started_at
            ) VALUES (?, ?, ?, ?, 'running', ?)
            ON CONFLICT(step_id) DO UPDATE SET
                state = 'running',
                started_at = excluded.started_at;
            """,
            (
                str(step_id),
                str(run_id),
                str(namespace_id),
                step_number,
                eff_started_at,
            ),
        )


def get_step(
    conn: sqlite3.Connection,
    step_id: StepId,
    namespace_id: NamespaceId,
) -> StepResult | None:
    """Retrieve a step result by ID, validating tenant namespace scoping."""
    row = conn.execute(
        "SELECT * FROM run_steps WHERE step_id = ?;",
        (str(step_id),),
    ).fetchone()

    if row is None:
        return None

    if row["namespace_id"] != str(namespace_id):
        raise CrossNamespaceAccessError(
            f"Cross-namespace access denied: step '{step_id}' belongs to "
            f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
            requested_namespace=str(namespace_id),
            target_namespace=str(row["namespace_id"]),
            resource_id=str(step_id),
        )

    return StepResult.from_json(row["step_result_json"])


def list_steps_for_run(
    conn: sqlite3.Connection,
    run_id: RunId,
    namespace_id: NamespaceId,
) -> tuple[StepResult, ...]:
    """List all steps for a run ordered by step_number, verifying namespace scoping."""
    run_row = conn.execute(
        "SELECT namespace_id FROM runs WHERE run_id = ?;",
        (str(run_id),),
    ).fetchone()

    if run_row is None:
        return ()

    if run_row["namespace_id"] != str(namespace_id):
        raise CrossNamespaceAccessError(
            f"Cross-namespace access denied: run '{run_id}' belongs to "
            f"namespace '{run_row['namespace_id']}', requested '{namespace_id}'",
            requested_namespace=str(namespace_id),
            target_namespace=str(run_row["namespace_id"]),
            resource_id=str(run_id),
        )

    rows = conn.execute(
        """
        SELECT step_result_json FROM run_steps
        WHERE run_id = ? AND namespace_id = ?
        ORDER BY step_number ASC;
        """,
        (str(run_id), str(namespace_id)),
    ).fetchall()

    return tuple(
        StepResult.from_json(r["step_result_json"])
        for r in rows
        if r["step_result_json"] is not None
    )


def update_run_status(
    conn: sqlite3.Connection,
    run_id: RunId,
    namespace_id: NamespaceId,
    status: RunStatus,
    *,
    error: str | None = None,
    failure_details: FailureDetails | None = None,
    completed_at: datetime | None = None,
    validator: PrePersistenceValidator | None = None,
) -> None:
    """Update execution run status, validating tenant namespace scoping."""
    with transaction(conn):
        row = conn.execute(
            "SELECT namespace_id, metadata_json FROM runs WHERE run_id = ?;",
            (str(run_id),),
        ).fetchone()

        if row is None:
            raise RecordNotFoundError(
                f"Run '{run_id}' not found in namespace '{namespace_id}'",
                record_id=str(run_id),
                namespace_id=str(namespace_id),
                table="runs",
            )

        if row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Cross-namespace access denied: run '{run_id}' belongs to "
                f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(row["namespace_id"]),
                resource_id=str(run_id),
            )

        meta_raw = row["metadata_json"]
        meta = json.loads(meta_raw) if meta_raw else {}

        if status in (RunStatus.FAILED, RunStatus.ABORTED):
            if error is None and failure_details is None:
                error = f"Run execution {status.value}"
            meta["error"] = error
            meta["failure_details"] = failure_details.to_dict() if failure_details else None
        elif status == RunStatus.COMPLETED:
            meta["error"] = None
            meta["failure_details"] = None
        else:
            if error is not None:
                meta["error"] = error
            if failure_details is not None:
                meta["failure_details"] = failure_details.to_dict()

        metadata_json = json.dumps(meta, sort_keys=True, separators=(",", ":"))
        metadata_json = str(
            resolve_validator(validator).sanitize_json_text(
                metadata_json,
                namespace_id=str(namespace_id),
                field_name="runs.metadata_json",
                record_id=str(run_id),
            )
        )

        conn.execute(
            """
            UPDATE runs
            SET status = ?, completed_at = ?, metadata_json = ?
            WHERE run_id = ? AND namespace_id = ?;
            """,
            (
                status.value,
                completed_at.astimezone(UTC).isoformat() if completed_at else None,
                metadata_json,
                str(run_id),
                str(namespace_id),
            ),
        )


def list_orphaned_runs(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId | None = None,
) -> tuple[RunResult, ...]:
    """List execution runs left in non-terminal states ('running', 'initialized').

    Args:
        conn: Target SQLite connection.
        namespace_id: Optional tenant namespace filter.

    Returns:
        Tuple of reconstructed RunResult contracts for all matching orphaned runs.
    """
    if namespace_id is not None:
        rows = conn.execute(
            """
            SELECT run_id, namespace_id FROM runs
            WHERE namespace_id = ? AND status IN ('running', 'initialized')
            ORDER BY created_at ASC;
            """,
            (str(namespace_id),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT run_id, namespace_id FROM runs
            WHERE status IN ('running', 'initialized')
            ORDER BY created_at ASC;
            """
        ).fetchall()

    results: list[RunResult] = []
    for r in rows:
        run_res = get_run(conn, RunId(str(r["run_id"])), NamespaceId(str(r["namespace_id"])))
        if run_res is not None:
            results.append(run_res)
    return tuple(results)


def list_running_steps_for_run(
    conn: sqlite3.Connection,
    run_id: RunId,
    namespace_id: NamespaceId,
) -> tuple[StepId, ...]:
    """Find all step IDs currently in 'running' state for a run."""
    run_row = conn.execute(
        "SELECT namespace_id FROM runs WHERE run_id = ?;",
        (str(run_id),),
    ).fetchone()

    if run_row is None:
        return ()

    if run_row["namespace_id"] != str(namespace_id):
        raise CrossNamespaceAccessError(
            f"Cross-namespace access denied: run '{run_id}' belongs to "
            f"namespace '{run_row['namespace_id']}', requested '{namespace_id}'",
            requested_namespace=str(namespace_id),
            target_namespace=str(run_row["namespace_id"]),
            resource_id=str(run_id),
        )

    rows = conn.execute(
        """
        SELECT step_id FROM run_steps
        WHERE run_id = ? AND namespace_id = ? AND state = 'running'
        ORDER BY step_number ASC;
        """,
        (str(run_id), str(namespace_id)),
    ).fetchall()
    return tuple(StepId(str(r["step_id"])) for r in rows)


def fail_orphaned_step(
    conn: sqlite3.Connection,
    step_id: StepId,
    namespace_id: NamespaceId,
    *,
    reason: str = "Active step orphaned due to process termination",
    failure_details: FailureDetails | None = None,
    completed_at: datetime | None = None,
    validator: PrePersistenceValidator | None = None,
) -> None:
    """Transition an in-flight running step to FAILED state upon crash recovery."""
    with transaction(conn):
        row = conn.execute(
            """
            SELECT run_id, namespace_id, step_number, step_result_json
            FROM run_steps WHERE step_id = ?;
            """,
            (str(step_id),),
        ).fetchone()

        if row is None:
            raise RecordNotFoundError(
                f"Step '{step_id}' not found in namespace '{namespace_id}'",
                record_id=str(step_id),
                namespace_id=str(namespace_id),
                table="run_steps",
            )

        if row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Cross-namespace access denied: step '{step_id}' belongs to "
                f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(row["namespace_id"]),
                resource_id=str(step_id),
            )

        eff_completed_at = (
            completed_at.astimezone(UTC).isoformat()
            if completed_at
            else datetime.now(UTC).isoformat()
        )

        fd = failure_details or FailureDetails(
            error_type="OrphanedStepError",
            message=reason,
            step_id=step_id,
            error_code="ORPHANED_RUN_DETECTED",
            recovery_hint="Inspect step execution log and idempotency keys before retrying",
        )
        dummy_decision = ModelDecision(
            decision_id=f"dec_{step_id}",
            step_id=step_id,
            finish_reason=ModelFinishReason.ERROR,
            content=f"Interrupted by crash: {reason}",
            tool_calls=(),
        )
        step_res = StepResult(
            step_id=step_id,
            run_id=RunId(str(row["run_id"])),
            step_number=int(row["step_number"]),
            decision=dummy_decision,
            tool_results=(),
            memories_proposed=0,
            memories_committed=0,
            tokens_consumed=0,
            completed_at=completed_at or datetime.now(UTC),
            failure_details=fd,
        )

        conn.execute(
            """
            UPDATE run_steps
            SET state = 'failed', completed_at = ?, step_result_json = ?
            WHERE step_id = ? AND namespace_id = ?;
            """,
            (
                eff_completed_at,
                resolve_validator(validator).sanitize_json_text(
                    step_res.to_json(),
                    namespace_id=str(namespace_id),
                    field_name="run_steps.step_result_json",
                    record_id=str(step_id),
                ),
                str(step_id),
                str(namespace_id),
            ),
        )

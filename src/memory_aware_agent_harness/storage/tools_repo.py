"""SQLite repository for tool invocations, intent tracking, and side-effect reconciliation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    CrossNamespaceAccessError,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    SideEffectType,
    StepId,
    ToolCallStatus,
    ToolInvocationId,
    ToolResult,
)
from memory_aware_agent_harness.storage.connection import transaction
from memory_aware_agent_harness.storage.security import (
    PrePersistenceValidator,
    resolve_validator,
)


@dataclass(frozen=True)
class ToolInvocationRecord:
    """Durable record of a tool invocation lifecycle."""

    invocation_id: ToolInvocationId
    step_id: StepId
    run_id: RunId
    namespace_id: NamespaceId
    tool_name: str
    idempotency_key: str
    side_effect: SideEffectType
    status: ToolCallStatus
    arguments_json: str
    result_json: str | None
    error_text: str | None
    dispatched_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ToolInvocationRecord:
        """Construct record from an SQLite row."""
        dispatched_dt = datetime.fromisoformat(row["dispatched_at"])
        completed_raw = row["completed_at"]
        completed_dt = datetime.fromisoformat(completed_raw) if completed_raw else None
        return cls(
            invocation_id=ToolInvocationId(str(row["invocation_id"])),
            step_id=StepId(str(row["step_id"])),
            run_id=RunId(str(row["run_id"])),
            namespace_id=NamespaceId(str(row["namespace_id"])),
            tool_name=str(row["tool_name"]),
            idempotency_key=str(row["idempotency_key"]),
            side_effect=SideEffectType(row["side_effect"]),
            status=ToolCallStatus(row["status"]),
            arguments_json=str(row["arguments_json"]),
            result_json=str(row["result_json"]) if row["result_json"] is not None else None,
            error_text=str(row["error_text"]) if row["error_text"] is not None else None,
            dispatched_at=dispatched_dt,
            completed_at=completed_dt,
        )


def record_tool_intent(
    conn: sqlite3.Connection,
    *,
    invocation_id: ToolInvocationId,
    step_id: StepId,
    run_id: RunId,
    namespace_id: NamespaceId,
    tool_name: str,
    idempotency_key: str,
    side_effect: SideEffectType,
    arguments: Mapping[str, Any] | None = None,
    dispatched_at: datetime | None = None,
    validator: PrePersistenceValidator | None = None,
) -> None:
    """Record durable intent before executing a tool call with namespace scoping.

    Args:
        conn: Target SQLite connection.
        invocation_id: Tool invocation identifier.
        step_id: Parent execution step.
        run_id: Parent execution run.
        namespace_id: Tenant namespace.
        tool_name: Name of tool to execute.
        idempotency_key: Deduplication/idempotency key.
        side_effect: Side effect classification.
        arguments: Tool input parameters.
        dispatched_at: Intent creation timestamp.

    Raises:
        RecordNotFoundError: If parent run or step is not found.
        CrossNamespaceAccessError: If parent run, step, or invocation violates namespace boundary.
    """
    with transaction(conn):
        # Validate parent run
        run_row = conn.execute(
            "SELECT namespace_id FROM runs WHERE run_id = ?;",
            (str(run_id),),
        ).fetchone()

        if run_row is None:
            raise RecordNotFoundError(
                f"Parent run '{run_id}' not found for invocation '{invocation_id}'",
                record_id=str(run_id),
                namespace_id=str(namespace_id),
                table="runs",
            )

        if run_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Parent run '{run_id}' belongs to namespace '{run_row['namespace_id']}', "
                f"cannot record tool intent under namespace '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(run_row["namespace_id"]),
                resource_id=str(run_id),
            )

        # Validate parent step
        step_row = conn.execute(
            "SELECT namespace_id, run_id FROM run_steps WHERE step_id = ?;",
            (str(step_id),),
        ).fetchone()

        if step_row is None:
            raise RecordNotFoundError(
                f"Parent step '{step_id}' not found for invocation '{invocation_id}'",
                record_id=str(step_id),
                namespace_id=str(namespace_id),
                table="run_steps",
            )

        if step_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Parent step '{step_id}' belongs to namespace '{step_row['namespace_id']}', "
                f"cannot record tool intent under namespace '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(step_row["namespace_id"]),
                resource_id=str(step_id),
            )

        if step_row["run_id"] != str(run_id):
            raise ValueError(
                f"Parent step '{step_id}' belongs to run '{step_row['run_id']}', "
                f"expected run '{run_id}'"
            )

        # Validate existing invocation if present
        inv_row = conn.execute(
            "SELECT namespace_id FROM tool_invocations WHERE invocation_id = ?;",
            (str(invocation_id),),
        ).fetchone()

        if inv_row is not None and inv_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Tool invocation '{invocation_id}' belongs to namespace "
                f"'{inv_row['namespace_id']}', cannot mutate under namespace '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(inv_row["namespace_id"]),
                resource_id=str(invocation_id),
            )

        arguments_json = json.dumps(
            dict(arguments) if arguments else {},
            sort_keys=True,
            separators=(",", ":"),
        )
        arguments_json = str(
            resolve_validator(validator).sanitize_json_text(
                arguments_json,
                namespace_id=str(namespace_id),
                field_name="tool_invocations.arguments_json",
                record_id=str(invocation_id),
            )
        )
        eff_dispatched_at = (
            dispatched_at.astimezone(UTC).isoformat()
            if dispatched_at
            else datetime.now(UTC).isoformat()
        )

        conn.execute(
            """
            INSERT INTO tool_invocations (
                invocation_id, step_id, namespace_id, tool_name,
                arguments_json, side_effect, idempotency_key, status,
                result_json, error_text, dispatched_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
            ON CONFLICT(invocation_id) DO UPDATE SET
                step_id = excluded.step_id,
                tool_name = excluded.tool_name,
                arguments_json = excluded.arguments_json,
                side_effect = excluded.side_effect,
                idempotency_key = excluded.idempotency_key;
            """,
            (
                str(invocation_id),
                str(step_id),
                str(namespace_id),
                str(tool_name),
                arguments_json,
                side_effect.value,
                str(idempotency_key),
                ToolCallStatus.PENDING.value,
                eff_dispatched_at,
            ),
        )


def record_tool_result(
    conn: sqlite3.Connection,
    *,
    invocation_id: ToolInvocationId,
    namespace_id: NamespaceId,
    tool_result: ToolResult,
    completed_at: datetime | None = None,
    validator: PrePersistenceValidator | None = None,
) -> None:
    """Durable record of tool execution result or terminal error with namespace scoping.

    Args:
        conn: Target SQLite connection.
        invocation_id: Tool invocation identifier.
        namespace_id: Tenant namespace.
        tool_result: ToolResult outcome contract.
        completed_at: Completion timestamp.

    Raises:
        RecordNotFoundError: If invocation record is not found.
        CrossNamespaceAccessError: If invocation belongs to another namespace.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT namespace_id FROM tool_invocations WHERE invocation_id = ?;",
            (str(invocation_id),),
        ).fetchone()

        if row is None:
            raise RecordNotFoundError(
                f"Tool invocation '{invocation_id}' not found in namespace '{namespace_id}'",
                record_id=str(invocation_id),
                namespace_id=str(namespace_id),
                table="tool_invocations",
            )

        if row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Cross-namespace access denied: tool invocation '{invocation_id}' belongs to "
                f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(row["namespace_id"]),
                resource_id=str(invocation_id),
            )

        result_json = (
            json.dumps(
                dict(tool_result.result),
                default=lambda o: dict(o) if isinstance(o, Mapping) else str(o),
                sort_keys=True,
                separators=(",", ":"),
            )
            if tool_result.result is not None
            else None
        )
        val = resolve_validator(validator)
        result_json = val.sanitize_json_text(
            result_json,
            namespace_id=str(namespace_id),
            field_name="tool_invocations.result_json",
            record_id=str(invocation_id),
        )
        clean_error = val.sanitize_error_text(
            tool_result.error,
            namespace_id=str(namespace_id),
            field_name="tool_invocations.error_text",
            record_id=str(invocation_id),
        )
        eff_completed_at = (
            completed_at.astimezone(UTC).isoformat()
            if completed_at
            else datetime.now(UTC).isoformat()
        )

        conn.execute(
            """
            UPDATE tool_invocations
            SET status = ?, result_json = ?, error_text = ?, completed_at = ?
            WHERE invocation_id = ? AND namespace_id = ?;
            """,
            (
                tool_result.status.value,
                result_json,
                clean_error,
                eff_completed_at,
                str(invocation_id),
                str(namespace_id),
            ),
        )


def mark_invocation_unknown(
    conn: sqlite3.Connection,
    invocation_id: ToolInvocationId,
    namespace_id: NamespaceId,
    *,
    error: str = "Execution outcome uncertain due to failure or cancellation",
    completed_at: datetime | None = None,
    validator: PrePersistenceValidator | None = None,
) -> None:
    """Mark a tool invocation's outcome as unknown with explicit namespace scoping."""
    with transaction(conn):
        row = conn.execute(
            "SELECT namespace_id FROM tool_invocations WHERE invocation_id = ?;",
            (str(invocation_id),),
        ).fetchone()

        if row is None:
            raise RecordNotFoundError(
                f"Tool invocation '{invocation_id}' not found in namespace '{namespace_id}'",
                record_id=str(invocation_id),
                namespace_id=str(namespace_id),
                table="tool_invocations",
            )

        if row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Cross-namespace access denied: tool invocation '{invocation_id}' belongs to "
                f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(row["namespace_id"]),
                resource_id=str(invocation_id),
            )

        eff_completed_at = (
            completed_at.astimezone(UTC).isoformat()
            if completed_at
            else datetime.now(UTC).isoformat()
        )

        conn.execute(
            """
            UPDATE tool_invocations
            SET status = ?, error_text = ?, completed_at = ?
            WHERE invocation_id = ? AND namespace_id = ?;
            """,
            (
                ToolCallStatus.OUTCOME_UNKNOWN.value,
                resolve_validator(validator).sanitize_error_text(
                    error,
                    namespace_id=str(namespace_id),
                    field_name="tool_invocations.error_text",
                    record_id=str(invocation_id),
                ),
                eff_completed_at,
                str(invocation_id),
                str(namespace_id),
            ),
        )


def get_tool_invocation(
    conn: sqlite3.Connection,
    invocation_id: ToolInvocationId,
    namespace_id: NamespaceId,
) -> ToolInvocationRecord | None:
    """Retrieve a tool invocation record by ID, verifying tenant namespace scoping."""
    row = conn.execute(
        """
        SELECT ti.*, rs.run_id
        FROM tool_invocations ti
        JOIN run_steps rs ON ti.step_id = rs.step_id
        WHERE ti.invocation_id = ?;
        """,
        (str(invocation_id),),
    ).fetchone()

    if row is None:
        return None

    if row["namespace_id"] != str(namespace_id):
        raise CrossNamespaceAccessError(
            f"Cross-namespace access denied: tool invocation '{invocation_id}' belongs to "
            f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
            requested_namespace=str(namespace_id),
            target_namespace=str(row["namespace_id"]),
            resource_id=str(invocation_id),
        )

    return ToolInvocationRecord.from_row(row)


def find_invocation_by_idempotency_key(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId,
    tool_name: str | None = None,
    idempotency_key: str = "",
) -> ToolInvocationRecord | None:
    """Find the latest invocation for an idempotency key within a namespace."""
    if tool_name:
        row = conn.execute(
            """
            SELECT ti.*, rs.run_id
            FROM tool_invocations ti
            JOIN run_steps rs ON ti.step_id = rs.step_id
            WHERE ti.namespace_id = ? AND ti.tool_name = ? AND ti.idempotency_key = ?
            ORDER BY ti.dispatched_at DESC LIMIT 1;
            """,
            (str(namespace_id), str(tool_name), str(idempotency_key)),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT ti.*, rs.run_id
            FROM tool_invocations ti
            JOIN run_steps rs ON ti.step_id = rs.step_id
            WHERE ti.namespace_id = ? AND ti.idempotency_key = ?
            ORDER BY ti.dispatched_at DESC LIMIT 1;
            """,
            (str(namespace_id), str(idempotency_key)),
        ).fetchone()

    if row is None:
        return None

    return ToolInvocationRecord.from_row(row)


def list_invocations_for_step(
    conn: sqlite3.Connection,
    step_id: StepId,
    namespace_id: NamespaceId,
) -> tuple[ToolInvocationRecord, ...]:
    """List all tool invocations for a step with namespace verification."""
    step_row = conn.execute(
        "SELECT namespace_id FROM run_steps WHERE step_id = ?;",
        (str(step_id),),
    ).fetchone()

    if step_row is None:
        return ()

    if step_row["namespace_id"] != str(namespace_id):
        raise CrossNamespaceAccessError(
            f"Cross-namespace access denied: step '{step_id}' belongs to "
            f"namespace '{step_row['namespace_id']}', requested '{namespace_id}'",
            requested_namespace=str(namespace_id),
            target_namespace=str(step_row["namespace_id"]),
            resource_id=str(step_id),
        )

    rows = conn.execute(
        """
        SELECT ti.*, rs.run_id
        FROM tool_invocations ti
        JOIN run_steps rs ON ti.step_id = rs.step_id
        WHERE ti.step_id = ? AND ti.namespace_id = ?
        ORDER BY ti.dispatched_at ASC;
        """,
        (str(step_id), str(namespace_id)),
    ).fetchall()

    return tuple(ToolInvocationRecord.from_row(r) for r in rows)


def find_pending_or_unknown_invocations(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId,
    run_id: RunId | None = None,
) -> tuple[ToolInvocationRecord, ...]:
    """Find all pending or outcome-unknown tool invocations within a namespace."""
    if run_id is not None:
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
            SELECT ti.*, rs.run_id
            FROM tool_invocations ti
            JOIN run_steps rs ON ti.step_id = rs.step_id
            WHERE ti.namespace_id = ? AND rs.run_id = ?
              AND ti.status IN ('pending', 'outcome_unknown')
            ORDER BY ti.dispatched_at ASC;
            """,
            (str(namespace_id), str(run_id)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT ti.*, rs.run_id
            FROM tool_invocations ti
            JOIN run_steps rs ON ti.step_id = rs.step_id
            WHERE ti.namespace_id = ?
              AND ti.status IN ('pending', 'outcome_unknown')
            ORDER BY ti.dispatched_at ASC;
            """,
            (str(namespace_id),),
        ).fetchall()

    return tuple(ToolInvocationRecord.from_row(r) for r in rows)

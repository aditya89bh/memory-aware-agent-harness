"""SQLite repository for tool invocations, intent tracking, and side-effect reconciliation."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    CrossNamespaceAccessError,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    SerializationError,
    SideEffectType,
    StepId,
    ToolCallStatus,
    ToolInvocationId,
    ToolResult,
    to_canonical_json,
)
from memory_aware_agent_harness.storage.connection import transaction
from memory_aware_agent_harness.storage.security import (
    PrePersistenceValidator,
    resolve_validator,
)

_META_ARGUMENT_KEYS: frozenset[str] = frozenset(
    {"idempotency_key", "call_id", "_call_id", "_idempotency_key"}
)


def _canonicalize_for_fingerprint(val: Any) -> Any:
    if val is None or isinstance(val, bool | str):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise SerializationError(f"Float value must be finite, got {val}")
        return int(val) if val.is_integer() else val
    if isinstance(val, Mapping):
        return {
            str(k): _canonicalize_for_fingerprint(v)
            for k, v in sorted(val.items(), key=lambda item: str(item[0]))
            if str(k) not in _META_ARGUMENT_KEYS
        }
    if isinstance(val, Sequence) and not isinstance(val, str | bytes):
        return [_canonicalize_for_fingerprint(item) for item in val]
    if isinstance(val, set | frozenset):
        return sorted([_canonicalize_for_fingerprint(item) for item in val])
    raise SerializationError(f"Unsupported value of type {type(val).__name__} for serialization")


def compute_argument_fingerprint(arguments: Mapping[str, Any] | None) -> str:
    """Compute a canonical SHA-256 fingerprint for tool arguments.

    Excludes execution-level meta parameters such as model-supplied idempotency_key
    or call_id to enforce that external operations are identified by their semantic payload.
    """
    if not arguments:
        return hashlib.sha256(b"{}").hexdigest()
    canonical = _canonicalize_for_fingerprint(arguments)
    serialized = to_canonical_json(canonical)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_argument_fingerprint_from_json(arguments_json: str | None) -> str:
    """Parse JSON arguments and compute canonical fingerprint."""
    if not arguments_json or not arguments_json.strip():
        return hashlib.sha256(b"{}").hexdigest()
    try:
        parsed = json.loads(arguments_json)
        if isinstance(parsed, Mapping):
            return compute_argument_fingerprint(parsed)
    except Exception:
        pass
    return hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()


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
    argument_fingerprint: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ToolInvocationRecord:
        """Construct record from an SQLite row."""
        dispatched_dt = datetime.fromisoformat(row["dispatched_at"])
        completed_raw = row["completed_at"]
        completed_dt = datetime.fromisoformat(completed_raw) if completed_raw else None
        row_dict = dict(row)
        fingerprint = str(row_dict.get("argument_fingerprint") or "")
        if not fingerprint and row_dict.get("arguments_json"):
            fingerprint = compute_argument_fingerprint_from_json(str(row_dict["arguments_json"]))

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
            argument_fingerprint=fingerprint,
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
                "Cross-namespace access denied",
                requested_namespace=str(namespace_id),
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
                "Cross-namespace access denied",
                requested_namespace=str(namespace_id),
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
                "Cross-namespace access denied",
                requested_namespace=str(namespace_id),
                resource_id=str(invocation_id),
            )

        arguments_json = to_canonical_json(arguments if arguments is not None else {})
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

        argument_fingerprint = compute_argument_fingerprint(arguments)

        # Check if an invocation for this idempotency key already exists under this namespace & tool
        existing_key_row = conn.execute(
            """
            SELECT invocation_id FROM tool_invocations
            WHERE namespace_id = ? AND tool_name = ? AND idempotency_key = ?;
            """,
            (str(namespace_id), str(tool_name), str(idempotency_key)),
        ).fetchone()

        if existing_key_row is not None and existing_key_row["invocation_id"] != str(invocation_id):
            try:
                conn.execute(
                    """
                    UPDATE tool_invocations SET
                        invocation_id = ?,
                        step_id = ?,
                        arguments_json = ?,
                        argument_fingerprint = ?,
                        side_effect = ?,
                        status = ?,
                        dispatched_at = ?,
                        result_json = NULL,
                        error_text = NULL,
                        completed_at = NULL
                    WHERE namespace_id = ? AND tool_name = ? AND idempotency_key = ?;
                    """,
                    (
                        str(invocation_id),
                        str(step_id),
                        arguments_json,
                        argument_fingerprint,
                        side_effect.value,
                        ToolCallStatus.PENDING.value,
                        eff_dispatched_at,
                        str(namespace_id),
                        str(tool_name),
                        str(idempotency_key),
                    ),
                )
                return
            except sqlite3.OperationalError as exc:
                if "has no column named argument_fingerprint" in str(exc):
                    conn.execute(
                        "ALTER TABLE tool_invocations "
                        "ADD COLUMN argument_fingerprint TEXT NOT NULL DEFAULT '';"
                    )
                    conn.execute(
                        """
                        UPDATE tool_invocations SET
                            invocation_id = ?,
                            step_id = ?,
                            arguments_json = ?,
                            argument_fingerprint = ?,
                            side_effect = ?,
                            status = ?,
                            dispatched_at = ?,
                            result_json = NULL,
                            error_text = NULL,
                            completed_at = NULL
                        WHERE namespace_id = ? AND tool_name = ? AND idempotency_key = ?;
                        """,
                        (
                            str(invocation_id),
                            str(step_id),
                            arguments_json,
                            argument_fingerprint,
                            side_effect.value,
                            ToolCallStatus.PENDING.value,
                            eff_dispatched_at,
                            str(namespace_id),
                            str(tool_name),
                            str(idempotency_key),
                        ),
                    )
                    return
                raise

        try:
            conn.execute(
                """
                INSERT INTO tool_invocations (
                    invocation_id, step_id, namespace_id, tool_name,
                    arguments_json, argument_fingerprint, side_effect, idempotency_key, status,
                    result_json, error_text, dispatched_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
                ON CONFLICT(invocation_id) DO UPDATE SET
                    step_id = excluded.step_id,
                    tool_name = excluded.tool_name,
                    arguments_json = excluded.arguments_json,
                    argument_fingerprint = excluded.argument_fingerprint,
                    side_effect = excluded.side_effect,
                    idempotency_key = excluded.idempotency_key;
                """,
                (
                    str(invocation_id),
                    str(step_id),
                    str(namespace_id),
                    str(tool_name),
                    arguments_json,
                    argument_fingerprint,
                    side_effect.value,
                    str(idempotency_key),
                    ToolCallStatus.PENDING.value,
                    eff_dispatched_at,
                ),
            )
        except sqlite3.OperationalError as exc:
            if "has no column named argument_fingerprint" in str(exc):
                conn.execute(
                    "ALTER TABLE tool_invocations "
                    "ADD COLUMN argument_fingerprint TEXT NOT NULL DEFAULT '';"
                )
                conn.execute(
                    """
                    INSERT INTO tool_invocations (
                        invocation_id, step_id, namespace_id, tool_name,
                        arguments_json, argument_fingerprint, side_effect, idempotency_key, status,
                        result_json, error_text, dispatched_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
                    ON CONFLICT(invocation_id) DO UPDATE SET
                        step_id = excluded.step_id,
                        tool_name = excluded.tool_name,
                        arguments_json = excluded.arguments_json,
                        argument_fingerprint = excluded.argument_fingerprint,
                        side_effect = excluded.side_effect,
                        idempotency_key = excluded.idempotency_key;
                    """,
                    (
                        str(invocation_id),
                        str(step_id),
                        str(namespace_id),
                        str(tool_name),
                        arguments_json,
                        argument_fingerprint,
                        side_effect.value,
                        str(idempotency_key),
                        ToolCallStatus.PENDING.value,
                        eff_dispatched_at,
                    ),
                )
            else:
                raise


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
                "Cross-namespace access denied",
                requested_namespace=str(namespace_id),
                resource_id=str(invocation_id),
            )

        result_json = (
            to_canonical_json(tool_result.result) if tool_result.result is not None else None
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
                "Cross-namespace access denied",
                requested_namespace=str(namespace_id),
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
        WHERE ti.invocation_id = ? AND ti.namespace_id = ?;
        """,
        (str(invocation_id), str(namespace_id)),
    ).fetchone()

    if row is None:
        return None

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
        "SELECT namespace_id FROM run_steps WHERE step_id = ? AND namespace_id = ?;",
        (str(step_id), str(namespace_id)),
    ).fetchone()

    if step_row is None:
        return ()

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
            "SELECT namespace_id FROM runs WHERE run_id = ? AND namespace_id = ?;",
            (str(run_id), str(namespace_id)),
        ).fetchone()

        if run_row is None:
            return ()

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


def find_uncertain_invocation_by_fingerprint(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId,
    tool_name: str,
    argument_fingerprint: str,
    run_id: RunId | None = None,
) -> ToolInvocationRecord | None:
    """Find the most recent pending or outcome-unknown invocation matching fingerprint."""
    if run_id is not None:
        run_row = conn.execute(
            "SELECT namespace_id FROM runs WHERE run_id = ? AND namespace_id = ?;",
            (str(run_id), str(namespace_id)),
        ).fetchone()
        if run_row is None:
            return None
        rows = conn.execute(
            """
            SELECT ti.*, rs.run_id
            FROM tool_invocations ti
            JOIN run_steps rs ON ti.step_id = rs.step_id
            WHERE ti.namespace_id = ? AND rs.run_id = ? AND ti.tool_name = ?
              AND ti.status IN ('pending', 'outcome_unknown')
            ORDER BY ti.dispatched_at DESC;
            """,
            (str(namespace_id), str(run_id), str(tool_name)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT ti.*, rs.run_id
            FROM tool_invocations ti
            JOIN run_steps rs ON ti.step_id = rs.step_id
            WHERE ti.namespace_id = ? AND ti.tool_name = ?
              AND ti.status IN ('pending', 'outcome_unknown')
            ORDER BY ti.dispatched_at DESC;
            """,
            (str(namespace_id), str(tool_name)),
        ).fetchall()

    for r in rows:
        rec = ToolInvocationRecord.from_row(r)
        if rec.argument_fingerprint == argument_fingerprint:
            return rec
    return None

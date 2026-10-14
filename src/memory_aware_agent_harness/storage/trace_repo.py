"""SQLite repository for append-only execution trace events and ordered audit history."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    CrossNamespaceAccessError,
    EventId,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    SerializableRecord,
    StepId,
    compute_state_hash,
    verify_state_hash,
)
from memory_aware_agent_harness.storage.connection import transaction


@dataclass(frozen=True)
class TraceEventRecord:
    """Immutable trace event record in execution audit log."""

    event_id: EventId
    run_id: RunId
    namespace_id: NamespaceId
    sequence_num: int
    event_type: str
    payload_json: str
    state_hash: str
    emitted_at: datetime
    step_id: StepId | None = None

    def payload(self) -> dict[str, Any]:
        """Deserialize payload dictionary from canonical JSON string."""
        raw = json.loads(self.payload_json)
        return dict(raw) if isinstance(raw, dict) else {"value": raw}

    def verify_integrity(self, *, raise_on_divergence: bool = True) -> bool:
        """Verify that event payload matches state_hash."""
        return verify_state_hash(
            self.payload(),
            self.state_hash,
            raise_on_divergence=raise_on_divergence,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TraceEventRecord:
        """Construct record from an SQLite row."""
        step_raw = row["step_id"]
        return cls(
            event_id=EventId(str(row["event_id"])),
            run_id=RunId(str(row["run_id"])),
            namespace_id=NamespaceId(str(row["namespace_id"])),
            sequence_num=int(row["sequence_num"]),
            event_type=str(row["event_type"]),
            payload_json=str(row["payload_json"]),
            state_hash=str(row["state_hash"]),
            emitted_at=datetime.fromisoformat(row["emitted_at"]),
            step_id=StepId(str(step_raw)) if step_raw else None,
        )


def append_trace_event(
    conn: sqlite3.Connection,
    *,
    event_id: EventId,
    run_id: RunId,
    namespace_id: NamespaceId,
    sequence_num: int,
    event_type: str,
    payload: Mapping[str, Any] | SerializableRecord,
    emitted_at: datetime | None = None,
    step_id: StepId | None = None,
) -> TraceEventRecord:
    """Append an immutable trace event to the audit log with strict namespace scoping.

    Args:
        conn: Target SQLite connection.
        event_id: Unique event identifier.
        run_id: Parent execution run.
        namespace_id: Tenant namespace.
        sequence_num: Monotonic sequence index within the run.
        event_type: Categorical event type descriptor.
        payload: Event payload dictionary or serializable record.
        emitted_at: Event timestamp.
        step_id: Optional step identifier associated with this event.

    Returns:
        Persisted TraceEventRecord.

    Raises:
        RecordNotFoundError: If parent run or step is not found.
        CrossNamespaceAccessError: If access violates tenant namespace boundary.
        ValueError: If sequence_num < 0 or step does not match run.
    """
    if sequence_num < 0:
        raise ValueError(f"sequence_num must be non-negative, got {sequence_num}")

    with transaction(conn):
        # Validate parent run
        run_row = conn.execute(
            "SELECT namespace_id FROM runs WHERE run_id = ?;",
            (str(run_id),),
        ).fetchone()

        if run_row is None:
            raise RecordNotFoundError(
                f"Parent run '{run_id}' not found for trace event '{event_id}'",
                record_id=str(run_id),
                namespace_id=str(namespace_id),
                table="runs",
            )

        if run_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Parent run '{run_id}' belongs to namespace '{run_row['namespace_id']}', "
                f"cannot append trace event under namespace '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(run_row["namespace_id"]),
                resource_id=str(run_id),
            )

        # Validate step if provided
        if step_id is not None:
            step_row = conn.execute(
                "SELECT namespace_id, run_id FROM run_steps WHERE step_id = ?;",
                (str(step_id),),
            ).fetchone()

            if step_row is None:
                raise RecordNotFoundError(
                    f"Parent step '{step_id}' not found for trace event '{event_id}'",
                    record_id=str(step_id),
                    namespace_id=str(namespace_id),
                    table="run_steps",
                )

            if step_row["namespace_id"] != str(namespace_id):
                raise CrossNamespaceAccessError(
                    f"Parent step '{step_id}' belongs to namespace '{step_row['namespace_id']}', "
                    f"cannot append trace event under namespace '{namespace_id}'",
                    requested_namespace=str(namespace_id),
                    target_namespace=str(step_row["namespace_id"]),
                    resource_id=str(step_id),
                )

            if step_row["run_id"] != str(run_id):
                raise ValueError(
                    f"Parent step '{step_id}' belongs to run '{step_row['run_id']}', "
                    f"expected run '{run_id}'"
                )

        # Validate existing event if present
        existing_row = conn.execute(
            "SELECT namespace_id FROM trace_events WHERE event_id = ?;",
            (str(event_id),),
        ).fetchone()

        if existing_row is not None and existing_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Trace event '{event_id}' belongs to namespace "
                f"'{existing_row['namespace_id']}', cannot mutate under namespace '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(existing_row["namespace_id"]),
                resource_id=str(event_id),
            )

        state_hash = compute_state_hash(payload)
        if isinstance(payload, SerializableRecord):
            payload_json = payload.to_json()
        else:
            payload_json = json.dumps(
                dict(payload),
                sort_keys=True,
                separators=(",", ":"),
            )

        emitted_dt = emitted_at.astimezone(UTC) if emitted_at else datetime.now(UTC)
        eff_emitted_at = emitted_dt.isoformat()

        conn.execute(
            """
            INSERT INTO trace_events (
                event_id, run_id, step_id, namespace_id, sequence_num,
                event_type, payload_json, state_hash, emitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                step_id = excluded.step_id,
                sequence_num = excluded.sequence_num,
                event_type = excluded.event_type,
                payload_json = excluded.payload_json,
                state_hash = excluded.state_hash,
                emitted_at = excluded.emitted_at;
            """,
            (
                str(event_id),
                str(run_id),
                str(step_id) if step_id else None,
                str(namespace_id),
                sequence_num,
                str(event_type),
                payload_json,
                state_hash,
                eff_emitted_at,
            ),
        )

        return TraceEventRecord(
            event_id=event_id,
            run_id=run_id,
            namespace_id=namespace_id,
            sequence_num=sequence_num,
            event_type=event_type,
            payload_json=payload_json,
            state_hash=state_hash,
            emitted_at=emitted_dt,
            step_id=step_id,
        )


def get_trace_event(
    conn: sqlite3.Connection,
    event_id: EventId,
    namespace_id: NamespaceId,
    *,
    verify_hash: bool = True,
) -> TraceEventRecord | None:
    """Retrieve a single trace event by ID with namespace scoping and integrity verification."""
    row = conn.execute(
        "SELECT * FROM trace_events WHERE event_id = ?;",
        (str(event_id),),
    ).fetchone()

    if row is None:
        return None

    if row["namespace_id"] != str(namespace_id):
        raise CrossNamespaceAccessError(
            f"Cross-namespace access denied: trace event '{event_id}' belongs to "
            f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
            requested_namespace=str(namespace_id),
            target_namespace=str(row["namespace_id"]),
            resource_id=str(event_id),
        )

    record = TraceEventRecord.from_row(row)
    if verify_hash:
        record.verify_integrity(raise_on_divergence=True)

    return record


def get_trace_events(
    conn: sqlite3.Connection,
    run_id: RunId,
    namespace_id: NamespaceId,
    *,
    from_sequence: int = 0,
    limit: int | None = None,
    verify_hash: bool = False,
) -> tuple[TraceEventRecord, ...]:
    """Retrieve trace events for a run ordered monotonically by sequence_num."""
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

    if limit is not None:
        rows = conn.execute(
            """
            SELECT * FROM trace_events
            WHERE namespace_id = ? AND run_id = ? AND sequence_num >= ?
            ORDER BY sequence_num ASC
            LIMIT ?;
            """,
            (str(namespace_id), str(run_id), from_sequence, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM trace_events
            WHERE namespace_id = ? AND run_id = ? AND sequence_num >= ?
            ORDER BY sequence_num ASC;
            """,
            (str(namespace_id), str(run_id), from_sequence),
        ).fetchall()

    records = tuple(TraceEventRecord.from_row(r) for r in rows)
    if verify_hash:
        for rec in records:
            rec.verify_integrity(raise_on_divergence=True)

    return records

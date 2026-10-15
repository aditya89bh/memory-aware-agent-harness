"""SQLite repository for execution checkpoints with state hash integrity and lineage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    BranchId,
    CheckpointId,
    CrossNamespaceAccessError,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    SerializableRecord,
    StepId,
    compute_state_hash,
    verify_state_hash,
)
from memory_aware_agent_harness.storage.connection import transaction
from memory_aware_agent_harness.storage.security import (
    PrePersistenceValidator,
    resolve_validator,
)

DEFAULT_BRANCH_ID: BranchId = BranchId("main")


@dataclass(frozen=True)
class CheckpointRecord:
    """Durable checkpoint snapshot with state hash verification."""

    checkpoint_id: CheckpointId
    run_id: RunId
    step_id: StepId
    branch_id: BranchId
    namespace_id: NamespaceId
    parent_checkpoint_id: CheckpointId | None
    state_payload_json: str
    state_hash: str
    created_at: datetime

    def state_payload(self) -> dict[str, Any]:
        """Deserialize payload dictionary from canonical JSON string."""
        raw = json.loads(self.state_payload_json)
        return dict(raw) if isinstance(raw, dict) else {"value": raw}

    def verify_integrity(self, *, raise_on_divergence: bool = True) -> bool:
        """Verify that state payload matches state_hash."""
        return verify_state_hash(
            self.state_payload(),
            self.state_hash,
            raise_on_divergence=raise_on_divergence,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> CheckpointRecord:
        """Construct record from an SQLite row."""
        parent_raw = row["parent_checkpoint_id"]
        return cls(
            checkpoint_id=CheckpointId(str(row["checkpoint_id"])),
            run_id=RunId(str(row["run_id"])),
            step_id=StepId(str(row["step_id"])),
            branch_id=BranchId(str(row["branch_id"])),
            namespace_id=NamespaceId(str(row["namespace_id"])),
            parent_checkpoint_id=CheckpointId(str(parent_raw)) if parent_raw else None,
            state_payload_json=str(row["state_payload_json"]),
            state_hash=str(row["state_hash"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


def save_checkpoint(
    conn: sqlite3.Connection,
    *,
    checkpoint_id: CheckpointId,
    run_id: RunId,
    step_id: StepId,
    branch_id: BranchId,
    namespace_id: NamespaceId,
    state_payload: Mapping[str, Any] | SerializableRecord,
    parent_checkpoint_id: CheckpointId | None = None,
    created_at: datetime | None = None,
    validator: PrePersistenceValidator | None = None,
) -> CheckpointRecord:
    """Persist a durable checkpoint snapshot with state hash computation.

    Args:
        conn: Target SQLite connection.
        checkpoint_id: Checkpoint identifier.
        run_id: Parent execution run identifier.
        step_id: Parent execution step identifier.
        branch_id: Branch identifier for lineage forks.
        namespace_id: Tenant namespace.
        state_payload: Structured state snapshot or serializable record.
        parent_checkpoint_id: Optional parent checkpoint in fork lineage.
        created_at: Checkpoint timestamp.

    Returns:
        Persisted CheckpointRecord.

    Raises:
        RecordNotFoundError: If parent run, step, or parent checkpoint does not exist.
        CrossNamespaceAccessError: If access violates tenant namespace boundary.
        ValueError: If step does not belong to the specified run.
    """
    with transaction(conn):
        # Validate parent run
        run_row = conn.execute(
            "SELECT namespace_id FROM runs WHERE run_id = ?;",
            (str(run_id),),
        ).fetchone()

        if run_row is None:
            raise RecordNotFoundError(
                f"Parent run '{run_id}' not found for checkpoint '{checkpoint_id}'",
                record_id=str(run_id),
                namespace_id=str(namespace_id),
                table="runs",
            )

        if run_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Parent run '{run_id}' belongs to namespace '{run_row['namespace_id']}', "
                f"cannot save checkpoint under namespace '{namespace_id}'",
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
                f"Parent step '{step_id}' not found for checkpoint '{checkpoint_id}'",
                record_id=str(step_id),
                namespace_id=str(namespace_id),
                table="run_steps",
            )

        if step_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Parent step '{step_id}' belongs to namespace '{step_row['namespace_id']}', "
                f"cannot save checkpoint under namespace '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(step_row["namespace_id"]),
                resource_id=str(step_id),
            )

        if step_row["run_id"] != str(run_id):
            raise ValueError(
                f"Parent step '{step_id}' belongs to run '{step_row['run_id']}', "
                f"expected run '{run_id}'"
            )

        # Validate parent checkpoint if specified
        if parent_checkpoint_id is not None:
            if str(parent_checkpoint_id) == str(checkpoint_id):
                raise ValueError(f"Checkpoint '{checkpoint_id}' cannot be its own parent")

            parent_row = conn.execute(
                "SELECT namespace_id, run_id FROM checkpoints WHERE checkpoint_id = ?;",
                (str(parent_checkpoint_id),),
            ).fetchone()

            if parent_row is None:
                raise RecordNotFoundError(
                    f"Parent checkpoint '{parent_checkpoint_id}' not found for "
                    f"checkpoint '{checkpoint_id}'",
                    record_id=str(parent_checkpoint_id),
                    namespace_id=str(namespace_id),
                    table="checkpoints",
                )

            if parent_row["namespace_id"] != str(namespace_id):
                raise CrossNamespaceAccessError(
                    f"Parent checkpoint '{parent_checkpoint_id}' belongs to namespace "
                    f"'{parent_row['namespace_id']}', cannot reference under "
                    f"namespace '{namespace_id}'",
                    requested_namespace=str(namespace_id),
                    target_namespace=str(parent_row["namespace_id"]),
                    resource_id=str(parent_checkpoint_id),
                )

            if parent_row["run_id"] != str(run_id):
                raise ValueError(
                    f"Parent checkpoint '{parent_checkpoint_id}' belongs to run "
                    f"'{parent_row['run_id']}', cannot be referenced from run '{run_id}'"
                )

        # Validate existing checkpoint if present
        existing_row = conn.execute(
            "SELECT namespace_id FROM checkpoints WHERE checkpoint_id = ?;",
            (str(checkpoint_id),),
        ).fetchone()

        if existing_row is not None and existing_row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Checkpoint '{checkpoint_id}' belongs to namespace "
                f"'{existing_row['namespace_id']}', cannot mutate under namespace '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(existing_row["namespace_id"]),
                resource_id=str(checkpoint_id),
            )

        state_hash = compute_state_hash(state_payload)
        if isinstance(state_payload, SerializableRecord):
            payload_json = state_payload.to_json()
        else:
            payload_json = json.dumps(
                dict(state_payload),
                sort_keys=True,
                separators=(",", ":"),
            )

        # Sanitize before SQL parameters exist; hash what is actually persisted so that
        # checkpoint integrity verification remains consistent after redaction.
        clean_payload_json = str(
            resolve_validator(validator).sanitize_json_text(
                payload_json,
                namespace_id=str(namespace_id),
                field_name="checkpoints.state_payload_json",
                record_id=str(checkpoint_id),
            )
        )
        if clean_payload_json != payload_json:
            payload_json = clean_payload_json
            state_hash = compute_state_hash(json.loads(payload_json))

        created_dt = created_at.astimezone(UTC) if created_at else datetime.now(UTC)
        eff_created_at = created_dt.isoformat()

        conn.execute(
            """
            INSERT INTO checkpoints (
                checkpoint_id, run_id, step_id, branch_id, namespace_id,
                parent_checkpoint_id, state_payload_json, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(checkpoint_id) DO UPDATE SET
                run_id = excluded.run_id,
                step_id = excluded.step_id,
                branch_id = excluded.branch_id,
                parent_checkpoint_id = excluded.parent_checkpoint_id,
                state_payload_json = excluded.state_payload_json,
                state_hash = excluded.state_hash,
                created_at = excluded.created_at;
            """,
            (
                str(checkpoint_id),
                str(run_id),
                str(step_id),
                str(branch_id),
                str(namespace_id),
                str(parent_checkpoint_id) if parent_checkpoint_id else None,
                payload_json,
                state_hash,
                eff_created_at,
            ),
        )

        return CheckpointRecord(
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            step_id=step_id,
            branch_id=branch_id,
            namespace_id=namespace_id,
            parent_checkpoint_id=parent_checkpoint_id,
            state_payload_json=payload_json,
            state_hash=state_hash,
            created_at=created_dt,
        )


def get_checkpoint(
    conn: sqlite3.Connection,
    checkpoint_id: CheckpointId,
    namespace_id: NamespaceId,
    *,
    verify_hash: bool = True,
) -> CheckpointRecord | None:
    """Retrieve checkpoint by ID with namespace verification and hash integrity check.

    Args:
        conn: Target SQLite connection.
        checkpoint_id: Checkpoint identifier.
        namespace_id: Tenant namespace.
        verify_hash: When True, verifies cryptographic payload integrity against state_hash.

    Returns:
        CheckpointRecord or None if not found.

    Raises:
        CrossNamespaceAccessError: If checkpoint belongs to a different namespace.
        ReplayDivergenceError: If verify_hash is True and payload hash differs from state_hash.
    """
    row = conn.execute(
        "SELECT * FROM checkpoints WHERE checkpoint_id = ?;",
        (str(checkpoint_id),),
    ).fetchone()

    if row is None:
        return None

    if row["namespace_id"] != str(namespace_id):
        raise CrossNamespaceAccessError(
            f"Cross-namespace access denied: checkpoint '{checkpoint_id}' belongs to "
            f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
            requested_namespace=str(namespace_id),
            target_namespace=str(row["namespace_id"]),
            resource_id=str(checkpoint_id),
        )

    record = CheckpointRecord.from_row(row)
    if verify_hash:
        record.verify_integrity(raise_on_divergence=True)

    return record


def get_latest_checkpoint(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId,
    run_id: RunId,
    branch_id: BranchId = DEFAULT_BRANCH_ID,
    *,
    verify_hash: bool = True,
) -> CheckpointRecord | None:
    """Retrieve the latest checkpoint for a run branch with namespace verification."""
    run_row = conn.execute(
        "SELECT namespace_id FROM runs WHERE run_id = ?;",
        (str(run_id),),
    ).fetchone()

    if run_row is None:
        return None

    if run_row["namespace_id"] != str(namespace_id):
        raise CrossNamespaceAccessError(
            f"Cross-namespace access denied: run '{run_id}' belongs to "
            f"namespace '{run_row['namespace_id']}', requested '{namespace_id}'",
            requested_namespace=str(namespace_id),
            target_namespace=str(run_row["namespace_id"]),
            resource_id=str(run_id),
        )

    row = conn.execute(
        """
        SELECT * FROM checkpoints
        WHERE namespace_id = ? AND run_id = ? AND branch_id = ?
        ORDER BY created_at DESC, rowid DESC LIMIT 1;
        """,
        (str(namespace_id), str(run_id), str(branch_id)),
    ).fetchone()

    if row is None:
        return None

    record = CheckpointRecord.from_row(row)
    if verify_hash:
        record.verify_integrity(raise_on_divergence=True)

    return record


def list_checkpoints_for_run(
    conn: sqlite3.Connection,
    run_id: RunId,
    namespace_id: NamespaceId,
    branch_id: BranchId | None = None,
) -> tuple[CheckpointRecord, ...]:
    """List checkpoints for a run ordered by created_at."""
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

    if branch_id is not None:
        rows = conn.execute(
            """
            SELECT * FROM checkpoints
            WHERE namespace_id = ? AND run_id = ? AND branch_id = ?
            ORDER BY created_at ASC;
            """,
            (str(namespace_id), str(run_id), str(branch_id)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM checkpoints
            WHERE namespace_id = ? AND run_id = ?
            ORDER BY created_at ASC;
            """,
            (str(namespace_id), str(run_id)),
        ).fetchall()

    return tuple(CheckpointRecord.from_row(r) for r in rows)

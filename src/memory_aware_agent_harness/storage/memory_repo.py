"""SQLite repository for durable memory records, optimistic concurrency, and tombstones."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    BranchId,
    CrossNamespaceAccessError,
    MemoryFilter,
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    ScoredMemoryItem,
    StorageVersionConflictError,
    TombstoneActiveError,
    TombstoneRecord,
)
from memory_aware_agent_harness.storage.connection import transaction
from memory_aware_agent_harness.storage.scoring import score_and_rank_memories


def _row_to_memory_record(row: sqlite3.Row) -> MemoryRecord:
    """Convert an SQLite row to a MemoryRecord contract."""
    created_dt = datetime.fromisoformat(row["created_at"])
    updated_dt = datetime.fromisoformat(row["updated_at"])
    expires_raw = row["expires_at"]
    expires_dt = datetime.fromisoformat(expires_raw) if expires_raw else None

    src_refs_raw = row["source_references_json"]
    src_refs = tuple(json.loads(src_refs_raw)) if src_refs_raw else ()

    meta_raw = row["metadata_json"]
    metadata = json.loads(meta_raw) if meta_raw else {}

    run_raw = row["run_id"]
    branch_raw = row["branch_id"]

    return MemoryRecord(
        id=MemoryId(str(row["memory_id"])),
        namespace_id=NamespaceId(str(row["namespace_id"])),
        kind=MemoryKind(row["kind"]),
        content=str(row["content"]),
        state=MemoryState(row["state"]),
        confidence=float(row["confidence"]),
        salience=float(row["salience"]),
        created_at=created_dt,
        updated_at=updated_dt,
        expires_at=expires_dt,
        source_references=src_refs,
        metadata=metadata,
        access_count=int(row["access_count"]),
        version=int(row["version"]),
        origin=MemoryOrigin(row["origin"]),
        run_id=RunId(str(run_raw)) if run_raw else None,
        branch_id=BranchId(str(branch_raw)) if branch_raw else None,
    )


def put_memory(
    conn: sqlite3.Connection,
    record: MemoryRecord,
) -> None:
    """Store or update a memory record with tenant namespace and tombstone guards.

    Args:
        conn: Target SQLite connection.
        record: MemoryRecord contract to persist.

    Raises:
        TombstoneActiveError: If an active tombstone exists for this memory.
        CrossNamespaceAccessError: If record belongs to a different namespace.
    """
    with transaction(conn):
        # 1. Guard against active tombstones
        tomb_row = conn.execute(
            """
            SELECT tombstone_id, reason FROM tombstones
            WHERE target_memory_id = ? AND namespace_id = ?;
            """,
            (str(record.id), str(record.namespace_id)),
        ).fetchone()

        if tomb_row is not None:
            raise TombstoneActiveError(
                f"Cannot mutate memory '{record.id}': active tombstone exists "
                f"('{tomb_row['tombstone_id']}'): {tomb_row['reason']}",
                target_memory_id=str(record.id),
                namespace_id=str(record.namespace_id),
                tombstone_id=str(tomb_row["tombstone_id"]),
            )

        # 2. Guard against cross-namespace overwrites
        existing_row = conn.execute(
            "SELECT namespace_id FROM memory_records WHERE memory_id = ?;",
            (str(record.id),),
        ).fetchone()

        if existing_row is not None and existing_row["namespace_id"] != str(record.namespace_id):
            raise CrossNamespaceAccessError(
                f"Memory record '{record.id}' belongs to namespace "
                f"'{existing_row['namespace_id']}', cannot mutate under "
                f"namespace '{record.namespace_id}'",
                requested_namespace=str(record.namespace_id),
                target_namespace=str(existing_row["namespace_id"]),
                resource_id=str(record.id),
            )

        src_refs_json = (
            json.dumps(list(record.source_references), sort_keys=True, separators=(",", ":"))
            if record.source_references
            else None
        )
        metadata_json = (
            json.dumps(dict(record.metadata), sort_keys=True, separators=(",", ":"))
            if record.metadata
            else None
        )
        expires_iso = record.expires_at.astimezone(UTC).isoformat() if record.expires_at else None

        conn.execute(
            """
            INSERT INTO memory_records (
                memory_id, namespace_id, kind, content, state,
                confidence, salience, origin, source_references_json,
                access_count, version, run_id, branch_id,
                created_at, updated_at, expires_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                kind = excluded.kind,
                content = excluded.content,
                state = excluded.state,
                confidence = excluded.confidence,
                salience = excluded.salience,
                origin = excluded.origin,
                source_references_json = excluded.source_references_json,
                access_count = excluded.access_count,
                version = excluded.version,
                run_id = excluded.run_id,
                branch_id = excluded.branch_id,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at,
                metadata_json = excluded.metadata_json;
            """,
            (
                str(record.id),
                str(record.namespace_id),
                record.kind.value,
                record.content,
                record.state.value,
                record.confidence,
                record.salience,
                record.origin.value,
                src_refs_json,
                record.access_count,
                record.version,
                str(record.run_id) if record.run_id else None,
                str(record.branch_id) if record.branch_id else None,
                record.created_at.astimezone(UTC).isoformat(),
                record.updated_at.astimezone(UTC).isoformat(),
                expires_iso,
                metadata_json,
            ),
        )

        # Sync lineage relationships
        for ref_id in record.source_references:
            conn.execute(
                """
                INSERT INTO memory_lineage (
                    namespace_id, target_memory_id, source_reference_id, relationship
                )
                SELECT ?, ?, ?, 'derived_from'
                WHERE NOT EXISTS (
                    SELECT 1 FROM memory_lineage
                    WHERE namespace_id = ? AND target_memory_id = ? AND source_reference_id = ?
                );
                """,
                (
                    str(record.namespace_id),
                    str(record.id),
                    str(ref_id),
                    str(record.namespace_id),
                    str(record.id),
                    str(ref_id),
                ),
            )


def get_memory(
    conn: sqlite3.Connection,
    memory_id: MemoryId,
    namespace_id: NamespaceId,
    *,
    include_tombstoned: bool = False,
    as_of: datetime | None = None,
) -> MemoryRecord | None:
    """Fetch a single memory record by identifier within a namespace.

    Args:
        conn: Target SQLite connection.
        memory_id: Target memory identifier.
        namespace_id: Expected tenant namespace.
        include_tombstoned: When False, returns None if record is tombstoned
            or marked deleted/expired.
        as_of: Optional reference time to evaluate TTL expiration against.

    Returns:
        MemoryRecord or None if not found, tombstoned, or expired.

    Raises:
        CrossNamespaceAccessError: If record belongs to a different namespace.
    """
    row = conn.execute(
        "SELECT * FROM memory_records WHERE memory_id = ?;",
        (str(memory_id),),
    ).fetchone()

    if row is None:
        return None

    if row["namespace_id"] != str(namespace_id):
        raise CrossNamespaceAccessError(
            f"Cross-namespace access denied: memory '{memory_id}' belongs to "
            f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
            requested_namespace=str(namespace_id),
            target_namespace=str(row["namespace_id"]),
            resource_id=str(memory_id),
        )

    if not include_tombstoned:
        if row["state"] in (MemoryState.DELETED.value, MemoryState.EXPIRED.value):
            return None

        if as_of is not None and row["expires_at"] is not None:
            expires_dt = datetime.fromisoformat(row["expires_at"])
            if expires_dt <= as_of:
                return None

        tomb = conn.execute(
            "SELECT 1 FROM tombstones WHERE target_memory_id = ? AND namespace_id = ?;",
            (str(memory_id), str(namespace_id)),
        ).fetchone()
        if tomb is not None:
            return None

    return _row_to_memory_record(row)


def update_memory_optimistic(
    conn: sqlite3.Connection,
    record: MemoryRecord,
    expected_version: int,
) -> None:
    """Update a memory record conditionally verifying expected version for optimistic concurrency.

    Args:
        conn: Target SQLite connection.
        record: Target record with updated content.
        expected_version: Expected current version in database.

    Raises:
        RecordNotFoundError: If record is not found.
        CrossNamespaceAccessError: If record belongs to another namespace.
        TombstoneActiveError: If record has an active tombstone.
        StorageVersionConflictError: If actual version does not match expected_version.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT namespace_id, version, state FROM memory_records WHERE memory_id = ?;",
            (str(record.id),),
        ).fetchone()

        if row is None:
            raise RecordNotFoundError(
                f"Memory record '{record.id}' not found in namespace '{record.namespace_id}'",
                record_id=str(record.id),
                namespace_id=str(record.namespace_id),
                table="memory_records",
            )

        if row["namespace_id"] != str(record.namespace_id):
            raise CrossNamespaceAccessError(
                f"Cross-namespace access denied: memory '{record.id}' belongs to "
                f"namespace '{row['namespace_id']}', requested '{record.namespace_id}'",
                requested_namespace=str(record.namespace_id),
                target_namespace=str(row["namespace_id"]),
                resource_id=str(record.id),
            )

        # Tombstone guard
        tomb = conn.execute(
            """
            SELECT tombstone_id, reason FROM tombstones
            WHERE target_memory_id = ? AND namespace_id = ?;
            """,
            (str(record.id), str(record.namespace_id)),
        ).fetchone()
        if tomb is not None:
            raise TombstoneActiveError(
                f"Cannot update memory '{record.id}': active tombstone exists",
                target_memory_id=str(record.id),
                namespace_id=str(record.namespace_id),
                tombstone_id=str(tomb["tombstone_id"]),
            )

        actual_version = int(row["version"])
        if actual_version != expected_version:
            raise StorageVersionConflictError(
                f"Optimistic lock conflict on memory '{record.id}': expected version "
                f"{expected_version}, found version {actual_version}",
                record_id=str(record.id),
                expected_version=expected_version,
                actual_version=actual_version,
            )

        new_version = expected_version + 1
        src_refs_json = (
            json.dumps(list(record.source_references), sort_keys=True, separators=(",", ":"))
            if record.source_references
            else None
        )
        metadata_json = (
            json.dumps(dict(record.metadata), sort_keys=True, separators=(",", ":"))
            if record.metadata
            else None
        )
        expires_iso = record.expires_at.astimezone(UTC).isoformat() if record.expires_at else None

        cursor = conn.execute(
            """
            UPDATE memory_records
            SET kind = ?, content = ?, state = ?, confidence = ?,
                salience = ?, origin = ?, source_references_json = ?,
                access_count = ?, version = ?, run_id = ?, branch_id = ?,
                updated_at = ?, expires_at = ?, metadata_json = ?
            WHERE memory_id = ? AND namespace_id = ? AND version = ?;
            """,
            (
                record.kind.value,
                record.content,
                record.state.value,
                record.confidence,
                record.salience,
                record.origin.value,
                src_refs_json,
                record.access_count,
                new_version,
                str(record.run_id) if record.run_id else None,
                str(record.branch_id) if record.branch_id else None,
                record.updated_at.astimezone(UTC).isoformat(),
                expires_iso,
                metadata_json,
                str(record.id),
                str(record.namespace_id),
                expected_version,
            ),
        )

        if cursor.rowcount == 0:
            raise StorageVersionConflictError(
                f"Concurrent modification on memory '{record.id}' during update",
                record_id=str(record.id),
                expected_version=expected_version,
                actual_version=actual_version,
            )

        # Sync lineage relationships
        for ref_id in record.source_references:
            conn.execute(
                """
                INSERT INTO memory_lineage (
                    namespace_id, target_memory_id, source_reference_id, relationship
                )
                SELECT ?, ?, ?, 'derived_from'
                WHERE NOT EXISTS (
                    SELECT 1 FROM memory_lineage
                    WHERE namespace_id = ? AND target_memory_id = ? AND source_reference_id = ?
                );
                """,
                (
                    str(record.namespace_id),
                    str(record.id),
                    str(ref_id),
                    str(record.namespace_id),
                    str(record.id),
                    str(ref_id),
                ),
            )


def tombstone_memory(
    conn: sqlite3.Connection,
    memory_id: MemoryId,
    namespace_id: NamespaceId,
    reason: str,
    *,
    tombstone_id: str | None = None,
    tombstoned_at: datetime | None = None,
) -> str:
    """Logically delete and tombstone a memory record with verifiable audit preserving lineage.

    Args:
        conn: Target SQLite connection.
        memory_id: Target memory identifier.
        namespace_id: Tenant namespace.
        reason: Explanation for tombstoning.
        tombstone_id: Optional unique tombstone identifier.
        tombstoned_at: Tombstone creation timestamp.

    Returns:
        Created tombstone identifier.

    Raises:
        RecordNotFoundError: If record is not found in namespace.
        CrossNamespaceAccessError: If record belongs to another namespace.
        TombstoneActiveError: If record is already tombstoned.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT namespace_id FROM memory_records WHERE memory_id = ?;",
            (str(memory_id),),
        ).fetchone()

        if row is None:
            raise RecordNotFoundError(
                f"Memory record '{memory_id}' not found in namespace '{namespace_id}'",
                record_id=str(memory_id),
                namespace_id=str(namespace_id),
                table="memory_records",
            )

        if row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Cross-namespace access denied: memory '{memory_id}' belongs to "
                f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(row["namespace_id"]),
                resource_id=str(memory_id),
            )

        existing = conn.execute(
            "SELECT tombstone_id FROM tombstones WHERE target_memory_id = ? AND namespace_id = ?;",
            (str(memory_id), str(namespace_id)),
        ).fetchone()

        if existing is not None:
            raise TombstoneActiveError(
                f"Memory '{memory_id}' is already tombstoned",
                target_memory_id=str(memory_id),
                namespace_id=str(namespace_id),
                tombstone_id=str(existing["tombstone_id"]),
            )

        eff_tomb_id = tombstone_id or f"tomb_{memory_id}"
        tomb_dt = tombstoned_at.astimezone(UTC) if tombstoned_at else datetime.now(UTC)

        conn.execute(
            """
            INSERT INTO tombstones (
                tombstone_id, target_memory_id, namespace_id, reason, tombstoned_at
            ) VALUES (?, ?, ?, ?, ?);
            """,
            (
                eff_tomb_id,
                str(memory_id),
                str(namespace_id),
                reason,
                tomb_dt.isoformat(),
            ),
        )

        conn.execute(
            """
            UPDATE memory_records
            SET state = ?, updated_at = ?
            WHERE memory_id = ? AND namespace_id = ?;
            """,
            (
                MemoryState.DELETED.value,
                tomb_dt.isoformat(),
                str(memory_id),
                str(namespace_id),
            ),
        )

        return eff_tomb_id


def _row_to_tombstone_record(row: sqlite3.Row) -> TombstoneRecord:
    """Convert an SQLite row to a TombstoneRecord contract."""
    return TombstoneRecord(
        tombstone_id=str(row["tombstone_id"]),
        target_memory_id=MemoryId(str(row["target_memory_id"])),
        namespace_id=NamespaceId(str(row["namespace_id"])),
        reason=str(row["reason"]),
        tombstoned_at=datetime.fromisoformat(row["tombstoned_at"]),
    )


def is_tombstoned(
    conn: sqlite3.Connection,
    memory_id: MemoryId,
    namespace_id: NamespaceId,
) -> bool:
    """Return True if an active tombstone exists for this memory in namespace."""
    row = conn.execute(
        "SELECT 1 FROM tombstones WHERE target_memory_id = ? AND namespace_id = ?;",
        (str(memory_id), str(namespace_id)),
    ).fetchone()
    return row is not None


def get_tombstone(
    conn: sqlite3.Connection,
    memory_id: MemoryId,
    namespace_id: NamespaceId,
) -> TombstoneRecord | None:
    """Retrieve the tombstone record for a memory in a namespace, if one exists."""
    row = conn.execute(
        "SELECT * FROM tombstones WHERE target_memory_id = ? AND namespace_id = ?;",
        (str(memory_id), str(namespace_id)),
    ).fetchone()
    if row is None:
        return None
    return _row_to_tombstone_record(row)


def get_tombstones(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId,
    *,
    limit: int = 100,
) -> tuple[TombstoneRecord, ...]:
    """Retrieve recent tombstone audit records for a namespace."""
    rows = conn.execute(
        "SELECT * FROM tombstones WHERE namespace_id = ? ORDER BY tombstoned_at DESC LIMIT ?;",
        (str(namespace_id), limit),
    ).fetchall()
    return tuple(_row_to_tombstone_record(r) for r in rows)


def list_tombstoned_memory_ids(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId,
) -> frozenset[MemoryId]:
    """Return all tombstoned memory IDs in a namespace for anti-resurrection reconciliation."""
    rows = conn.execute(
        "SELECT target_memory_id FROM tombstones WHERE namespace_id = ?;",
        (str(namespace_id),),
    ).fetchall()
    return frozenset(MemoryId(r["target_memory_id"]) for r in rows)


def hard_delete_memory(
    conn: sqlite3.Connection,
    memory_id: MemoryId,
    namespace_id: NamespaceId,
    *,
    reason: str = "HARD_DELETE",
    tombstone_id: str | None = None,
    tombstoned_at: datetime | None = None,
) -> TombstoneRecord:
    """Execute software-level hard deletion with secure zeroing and durable tombstone audit.

    Conforms to SPEC.md Section 6.1 and ADR-0008:
    1. Validates record existence and namespace boundary.
    2. Inserts durable TombstoneRecord in `tombstones` table if not already present.
    3. Deletes row from `memory_records` with PRAGMA secure_delete enabled.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM memory_records WHERE memory_id = ?;",
            (str(memory_id),),
        ).fetchone()

        if row is None:
            existing_tomb = conn.execute(
                "SELECT * FROM tombstones WHERE target_memory_id = ? AND namespace_id = ?;",
                (str(memory_id), str(namespace_id)),
            ).fetchone()
            if existing_tomb is not None:
                return _row_to_tombstone_record(existing_tomb)
            raise RecordNotFoundError(
                f"Memory record '{memory_id}' not found in namespace '{namespace_id}'",
                record_id=str(memory_id),
                table="memory_records",
            )

        if row["namespace_id"] != str(namespace_id):
            raise CrossNamespaceAccessError(
                f"Cross-namespace access denied: memory '{memory_id}' belongs to "
                f"namespace '{row['namespace_id']}', requested '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(row["namespace_id"]),
                resource_id=str(memory_id),
            )

        eff_tomb_id = tombstone_id or f"tomb_{memory_id}"
        tomb_dt = tombstoned_at.astimezone(UTC) if tombstoned_at else datetime.now(UTC)

        existing_tomb = conn.execute(
            "SELECT * FROM tombstones WHERE target_memory_id = ? AND namespace_id = ?;",
            (str(memory_id), str(namespace_id)),
        ).fetchone()

        if existing_tomb is None:
            conn.execute(
                """
                INSERT INTO tombstones (
                    tombstone_id, target_memory_id, namespace_id, reason, tombstoned_at
                ) VALUES (?, ?, ?, ?, ?);
                """,
                (
                    eff_tomb_id,
                    str(memory_id),
                    str(namespace_id),
                    reason,
                    tomb_dt.isoformat(),
                ),
            )
            tomb_record = TombstoneRecord(
                tombstone_id=eff_tomb_id,
                target_memory_id=memory_id,
                namespace_id=namespace_id,
                reason=reason,
                tombstoned_at=tomb_dt,
            )
        else:
            tomb_record = _row_to_tombstone_record(existing_tomb)

        conn.execute("PRAGMA secure_delete = ON;")
        conn.execute(
            "DELETE FROM memory_records WHERE memory_id = ? AND namespace_id = ?;",
            (str(memory_id), str(namespace_id)),
        )

        return tomb_record


def purge_expired_memories(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId,
    as_of: datetime,
    *,
    hard_delete: bool = False,
    limit: int = 100,
) -> tuple[MemoryId, ...]:
    """Purge or mark expired memories older than as_of timestamp.

    Args:
        conn: Target SQLite connection.
        namespace_id: Target tenant namespace.
        as_of: Deterministic boundary timestamp.
        hard_delete: If True, executes PRAGMA secure_delete DELETE and creates tombstones;
                     if False, sets state = 'expired' (soft deletion).
        limit: Maximum records to purge in single batch.

    Returns:
        Tuple of affected MemoryIds.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of datetime must be timezone-aware")

    as_of_iso = as_of.astimezone(UTC).isoformat()

    with transaction(conn):
        rows = conn.execute(
            """
            SELECT memory_id FROM memory_records
            WHERE namespace_id = ?
              AND expires_at IS NOT NULL
              AND expires_at <= ?
              AND state != 'deleted'
            ORDER BY expires_at ASC
            LIMIT ?;
            """,
            (str(namespace_id), as_of_iso, limit),
        ).fetchall()

        if not rows:
            return ()

        expired_ids = tuple(MemoryId(r["memory_id"]) for r in rows)

        if hard_delete:
            conn.execute("PRAGMA secure_delete = ON;")
            for mem_id in expired_ids:
                existing = conn.execute(
                    "SELECT 1 FROM tombstones WHERE target_memory_id = ? AND namespace_id = ?;",
                    (str(mem_id), str(namespace_id)),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO tombstones (
                            tombstone_id, target_memory_id, namespace_id, reason, tombstoned_at
                        ) VALUES (?, ?, ?, ?, ?);
                        """,
                        (
                            f"tomb_ttl_{mem_id}",
                            str(mem_id),
                            str(namespace_id),
                            "TTL_EXPIRED",
                            as_of_iso,
                        ),
                    )
                conn.execute(
                    "DELETE FROM memory_records WHERE memory_id = ? AND namespace_id = ?;",
                    (str(mem_id), str(namespace_id)),
                )
        else:
            for mem_id in expired_ids:
                conn.execute(
                    """
                    UPDATE memory_records
                    SET state = ?, updated_at = ?
                    WHERE memory_id = ? AND namespace_id = ?;
                    """,
                    (
                        MemoryState.EXPIRED.value,
                        as_of_iso,
                        str(mem_id),
                        str(namespace_id),
                    ),
                )

        return expired_ids


def list_active_memories(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId,
    kind: MemoryKind,
    *,
    as_of: datetime | None = None,
) -> tuple[MemoryRecord, ...]:
    """List active memories for a given category and namespace, excluding expired/tombstoned.

    Args:
        conn: Target SQLite connection.
        namespace_id: Tenant namespace.
        kind: Memory category.
        as_of: Reference time for TTL expiration.

    Returns:
        Tuple of active MemoryRecords ordered by created_at.
    """
    if as_of is not None:
        as_of_iso = as_of.astimezone(UTC).isoformat()
        rows = conn.execute(
            """
            SELECT mr.* FROM memory_records mr
            WHERE mr.namespace_id = ?
              AND mr.kind = ?
              AND mr.state = 'active'
              AND (mr.expires_at IS NULL OR mr.expires_at > ?)
              AND NOT EXISTS (
                  SELECT 1 FROM tombstones t
                  WHERE t.target_memory_id = mr.memory_id
                    AND t.namespace_id = mr.namespace_id
              )
            ORDER BY mr.created_at ASC;
            """,
            (str(namespace_id), kind.value, as_of_iso),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT mr.* FROM memory_records mr
            WHERE mr.namespace_id = ?
              AND mr.kind = ?
              AND mr.state = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM tombstones t
                  WHERE t.target_memory_id = mr.memory_id
                    AND t.namespace_id = mr.namespace_id
              )
            ORDER BY mr.created_at ASC;
            """,
            (str(namespace_id), kind.value),
        ).fetchall()

    return tuple(_row_to_memory_record(r) for r in rows)


def get_memory_lineage(
    conn: sqlite3.Connection,
    target_memory_id: MemoryId,
    namespace_id: NamespaceId,
) -> tuple[str, ...]:
    """Retrieve source reference identifiers for a target memory in lineage."""
    rows = conn.execute(
        """
        SELECT source_reference_id FROM memory_lineage
        WHERE target_memory_id = ? AND namespace_id = ?
        ORDER BY id ASC;
        """,
        (str(target_memory_id), str(namespace_id)),
    ).fetchall()
    return tuple(str(r["source_reference_id"]) for r in rows)


def record_memory_lineage(
    conn: sqlite3.Connection,
    target_memory_id: MemoryId,
    namespace_id: NamespaceId,
    source_reference_id: str,
    relationship: str = "derived_from",
) -> None:
    """Record provenance relationship between memory and source reference."""
    conn.execute(
        """
        INSERT INTO memory_lineage (
            namespace_id, target_memory_id, source_reference_id, relationship
        ) VALUES (?, ?, ?, ?);
        """,
        (str(namespace_id), str(target_memory_id), str(source_reference_id), relationship),
    )


def search_memories(
    conn: sqlite3.Connection,
    query_filter: MemoryFilter,
) -> tuple[MemoryRecord, ...]:
    """Execute scoped, multi-dimensional search returning matching records.

    Filters across namespace, kinds, branch, run, origin, state, confidence,
    salience, TTL expiration, and text content, excluding tombstoned records.

    Args:
        conn: Target SQLite connection.
        query_filter: Scoped search criteria.

    Returns:
        Tuple of matching MemoryRecords ordered by salience and created_at.
    """
    clauses: list[str] = [
        "mr.namespace_id = ?",
        """NOT EXISTS (
            SELECT 1 FROM tombstones t
            WHERE t.target_memory_id = mr.memory_id
              AND t.namespace_id = mr.namespace_id
        )""",
    ]
    params: list[Any] = [str(query_filter.namespace_id)]

    if query_filter.state is not None:
        clauses.append("mr.state = ?")
        params.append(query_filter.state.value)

    if query_filter.kinds:
        placeholders = ", ".join("?" for _ in query_filter.kinds)
        clauses.append(f"mr.kind IN ({placeholders})")
        params.extend(k.value for k in query_filter.kinds)

    if query_filter.run_id is not None:
        clauses.append("mr.run_id = ?")
        params.append(str(query_filter.run_id))

    if query_filter.branch_id is not None:
        clauses.append("mr.branch_id = ?")
        params.append(str(query_filter.branch_id))

    if query_filter.origin is not None:
        clauses.append("mr.origin = ?")
        params.append(query_filter.origin.value)

    if query_filter.min_confidence is not None:
        clauses.append("mr.confidence >= ?")
        params.append(query_filter.min_confidence)

    if query_filter.min_salience is not None:
        clauses.append("mr.salience >= ?")
        params.append(query_filter.min_salience)

    if query_filter.as_of is not None:
        clauses.append("(mr.expires_at IS NULL OR mr.expires_at > ?)")
        params.append(query_filter.as_of.astimezone(UTC).isoformat())

    if query_filter.query_text and query_filter.query_text.strip():
        clauses.append("mr.content LIKE ?")
        params.append(f"%{query_filter.query_text.strip()}%")

    params.append(query_filter.limit)

    where_sql = " AND ".join(clauses)
    sql = f"""
    SELECT mr.* FROM memory_records mr
    WHERE {where_sql}
    ORDER BY mr.salience DESC, mr.created_at DESC
    LIMIT ?;
    """

    rows = conn.execute(sql, params).fetchall()
    return tuple(_row_to_memory_record(r) for r in rows)


def search_memories_basic(
    conn: sqlite3.Connection,
    namespace_id: NamespaceId,
    query: str,
    kinds: Sequence[MemoryKind],
    limit: int = 50,
) -> tuple[MemoryRecord, ...]:
    """Search active memories by text and categories conforming to MemoryStoreProtocol."""
    query_filter = MemoryFilter(
        namespace_id=namespace_id,
        kinds=tuple(kinds),
        query_text=query if query.strip() else None,
        limit=limit,
    )
    return search_memories(conn, query_filter)


def search_memories_scored(
    conn: sqlite3.Connection,
    query_filter: MemoryFilter,
    *,
    as_of: datetime | None = None,
) -> tuple[ScoredMemoryItem, ...]:
    """Execute scoped search and rank results with deterministic multi-factor scoring."""
    unranked = search_memories(conn, query_filter)
    return score_and_rank_memories(unranked, query_filter, as_of=as_of)

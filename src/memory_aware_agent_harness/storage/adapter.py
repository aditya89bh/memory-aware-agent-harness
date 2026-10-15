"""Unified SQLite storage adapter implementing StoragePort and MemoryStoreProtocol.

Provides a production-grade, thread-aware, WAL-backed SQLite persistence adapter
unifying memory, runs, steps, tool invocations, checkpoints, trace events, and
atomic 4-phase step commits.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from memory_aware_agent_harness.contracts import (
    BranchId,
    CheckpointId,
    EventId,
    MemoryFilter,
    MemoryId,
    MemoryKind,
    MemoryRecord,
    MemoryStoreProtocol,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    RunResult,
    ScoredMemoryItem,
    SideEffectType,
    StepId,
    StepResult,
    StorageError,
    StoragePort,
    ToolInvocationId,
    ToolResult,
)
from memory_aware_agent_harness.storage.checkpoints_repo import (
    DEFAULT_BRANCH_ID,
    CheckpointRecord,
    get_checkpoint,
    get_latest_checkpoint,
    save_checkpoint,
)
from memory_aware_agent_harness.storage.connection import (
    StorageSettings,
    create_connection,
    transaction,
)
from memory_aware_agent_harness.storage.memory_repo import (
    get_memory,
    get_memory_lineage,
    list_active_memories,
    put_memory,
    record_memory_lineage,
    search_memories_basic,
    search_memories_scored,
    tombstone_memory,
    update_memory_optimistic,
)
from memory_aware_agent_harness.storage.migrations import apply_migrations
from memory_aware_agent_harness.storage.runs_repo import (
    get_run,
    get_step,
    save_run,
    save_step,
)
from memory_aware_agent_harness.storage.security import PrePersistenceValidator
from memory_aware_agent_harness.storage.step_commit import (
    LineageEntry,
    StepCommitBundle,
    StepCommitOutcome,
    TraceEventInput,
    commit_step_4phase,
    commit_step_transaction,
)
from memory_aware_agent_harness.storage.tools_repo import (
    ToolInvocationRecord,
    find_invocation_by_idempotency_key,
    record_tool_intent,
    record_tool_result,
)
from memory_aware_agent_harness.storage.tools_repo import (
    get_tool_invocation as repo_get_tool_invocation,
)
from memory_aware_agent_harness.storage.trace_repo import (
    TraceEventRecord,
    append_trace_event,
)
from memory_aware_agent_harness.storage.trace_repo import (
    get_trace_events as repo_get_trace_events,
)


def _tool_invocation_record_to_result(rec: ToolInvocationRecord) -> ToolResult:
    """Convert a durable ToolInvocationRecord to a ToolResult contract instance."""
    result_val: Mapping[str, Any] | None = None
    if rec.result_json is not None:
        try:
            raw = json.loads(rec.result_json)
            result_val = dict(raw) if isinstance(raw, dict) else {"result": raw}
        except Exception:
            result_val = {"raw": rec.result_json}

    exec_time_ms = 0.0
    if rec.completed_at is not None and rec.dispatched_at is not None:
        exec_time_ms = max(0.0, (rec.completed_at - rec.dispatched_at).total_seconds() * 1000.0)

    return ToolResult(
        invocation_id=rec.invocation_id,
        tool_name=rec.tool_name,
        status=rec.status,
        side_effect=rec.side_effect,
        idempotency_key=rec.idempotency_key,
        result=result_val,
        error=rec.error_text,
        execution_time_ms=exec_time_ms,
    )


class SQLiteStorageAdapter(StoragePort, MemoryStoreProtocol):
    """Unified SQLite persistence adapter conforming to StoragePort and MemoryStoreProtocol.

    Encapsulates connection lifecycle, WAL settings, schema migrations, and
    cross-repository queries.
    """

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        settings: StorageSettings | None = None,
        auto_migrate: bool = True,
        clock: Callable[[], datetime] | None = None,
        validator: PrePersistenceValidator | None = None,
    ) -> None:
        """Initialize adapter with an existing connection or a path to create one.

        Args:
            database_path: File path or ':memory:' for SQLite database.
            connection: Optional pre-configured sqlite3.Connection.
            settings: StorageSettings for connection creation.
            auto_migrate: If True, applies all versioned schema migrations on initialization.
            clock: Optional injectable clock function returning timezone-aware datetime.
            validator: Optional PrePersistenceValidator for credential redaction and provenance.
        """
        self._clock = clock or (lambda: datetime.now(UTC))
        self._validator = validator or PrePersistenceValidator()
        self._closed = False

        if connection is not None:
            self._conn = connection
            self._owns_connection = False
        else:
            path = database_path or ":memory:"
            if settings is not None:
                self._conn = create_connection(
                    path,
                    timeout_seconds=settings.timeout_seconds,
                    synchronous=settings.synchronous,
                    secure_delete=settings.secure_delete,
                )
            else:
                self._conn = create_connection(path)
            self._owns_connection = True

        if auto_migrate:
            apply_migrations(self._conn)

    @property
    def conn(self) -> sqlite3.Connection:
        """Underlying active SQLite connection."""
        self._check_open()
        return self._conn

    def _check_open(self) -> None:
        if self._closed:
            raise StorageError("SQLiteStorageAdapter is closed", operation="check_open")

    def close(self) -> None:
        """Close storage connection if owned by this adapter."""
        if not self._closed:
            self._closed = True
            if self._owns_connection:
                self._conn.close()

    def __enter__(self) -> Self:
        self._check_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def transaction(self, mode: str = "IMMEDIATE") -> AbstractContextManager[sqlite3.Connection]:
        """Context manager for explicit SQLite transaction or savepoint."""
        self._check_open()
        return transaction(self._conn, mode=mode)

    # =========================================================================
    # Memory Operations (MemoryStoreProtocol & StoragePort)
    # =========================================================================

    def put(self, record: MemoryRecord) -> None:
        """Store or update a memory record with pre-persistence validation and redaction."""
        self._check_open()
        clean_record = self._validator.sanitize_memory_record(record)
        put_memory(self._conn, clean_record)

    def get(
        self,
        memory_id: MemoryId,
        namespace_id: NamespaceId,
        *,
        as_of: datetime | None = None,
    ) -> MemoryRecord | None:
        """Fetch a single memory record by identifier within a namespace."""
        self._check_open()
        return get_memory(self._conn, memory_id, namespace_id, as_of=as_of)

    def search(
        self,
        namespace_id: NamespaceId,
        query: str,
        kinds: Sequence[MemoryKind],
        limit: int,
        *,
        as_of: datetime | None = None,
    ) -> Sequence[MemoryRecord]:
        """Search memory records by query and category."""
        self._check_open()
        return search_memories_basic(
            self._conn,
            namespace_id=namespace_id,
            query=query,
            kinds=kinds,
            limit=limit,
        )

    def search_scored(self, query_filter: MemoryFilter) -> Sequence[ScoredMemoryItem]:
        """Execute scoped, filtered search returning scored records with component breakdown."""
        self._check_open()
        return search_memories_scored(self._conn, query_filter)

    def list_active(
        self,
        namespace_id: NamespaceId,
        kind: MemoryKind,
        *,
        as_of: datetime | None = None,
    ) -> Sequence[MemoryRecord]:
        """List active memories for a given category."""
        self._check_open()
        return list_active_memories(self._conn, namespace_id=namespace_id, kind=kind, as_of=as_of)

    def tombstone(
        self,
        memory_id: MemoryId,
        namespace_id: NamespaceId,
        reason: str,
        *,
        tombstone_id: str | None = None,
        tombstoned_at: datetime | None = None,
    ) -> None:
        """Logically delete and tombstone a memory record with verifiable audit."""
        self._check_open()
        effective_at = tombstoned_at or self._clock()
        tombstone_memory(
            self._conn,
            memory_id=memory_id,
            namespace_id=namespace_id,
            reason=reason,
            tombstone_id=tombstone_id,
            tombstoned_at=effective_at,
        )

    def update_optimistic(self, record: MemoryRecord, expected_version: int) -> None:
        """Update a memory record conditionally verifying the expected version."""
        self._check_open()
        clean_record = self._validator.sanitize_memory_record(record)
        update_memory_optimistic(self._conn, record=clean_record, expected_version=expected_version)

    def get_memory_lineage(
        self, target_memory_id: MemoryId, namespace_id: NamespaceId
    ) -> tuple[str, ...]:
        """Retrieve source reference IDs contributing to target memory."""
        self._check_open()
        return get_memory_lineage(self._conn, target_memory_id, namespace_id)

    def record_memory_lineage(
        self,
        target_memory_id: MemoryId,
        namespace_id: NamespaceId,
        source_reference_id: str,
        relationship: str = "derived_from",
    ) -> None:
        """Record provenance relationship between memory and source reference."""
        self._check_open()
        record_memory_lineage(
            self._conn,
            target_memory_id=target_memory_id,
            namespace_id=namespace_id,
            source_reference_id=source_reference_id,
            relationship=relationship,
        )

    # =========================================================================
    # Run & Step Operations (StoragePort)
    # =========================================================================

    def save_run(self, run_result: RunResult) -> None:
        """Persist or update run execution state."""
        self._check_open()
        save_run(self._conn, run_result)

    def get_run(self, run_id: RunId, namespace_id: NamespaceId) -> RunResult | None:
        """Retrieve run state by identifier within a namespace."""
        self._check_open()
        return get_run(self._conn, run_id, namespace_id)

    def save_step(self, step_result: StepResult, namespace_id: NamespaceId) -> None:
        """Persist a single step execution outcome."""
        self._check_open()
        save_step(self._conn, step_result, namespace_id)

    def get_step(self, step_id: StepId, namespace_id: NamespaceId) -> StepResult | None:
        """Retrieve step execution result by identifier within a namespace."""
        self._check_open()
        return get_step(self._conn, step_id, namespace_id)

    # =========================================================================
    # Tool Invocations (StoragePort)
    # =========================================================================

    def record_tool_intent(
        self,
        invocation_id: ToolInvocationId,
        step_id: StepId,
        namespace_id: NamespaceId,
        tool_name: str,
        arguments: Mapping[str, Any],
        side_effect: SideEffectType,
        idempotency_key: str,
        dispatched_at: datetime,
        run_id: RunId | None = None,
    ) -> None:
        """Record intent before tool dispatch for crash safety."""
        self._check_open()
        resolved_run_id = run_id
        if resolved_run_id is None:
            row = self._conn.execute(
                "SELECT run_id FROM run_steps WHERE step_id = ?;",
                (str(step_id),),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(
                    f"Parent step '{step_id}' not found for tool invocation '{invocation_id}'",
                    record_id=str(step_id),
                    namespace_id=str(namespace_id),
                    table="run_steps",
                )
            resolved_run_id = RunId(str(row["run_id"]))

        record_tool_intent(
            self._conn,
            invocation_id=invocation_id,
            step_id=step_id,
            run_id=resolved_run_id,
            namespace_id=namespace_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            side_effect=side_effect,
            arguments=arguments,
            dispatched_at=dispatched_at,
        )

    def record_tool_result(
        self,
        result: ToolResult,
        namespace_id: NamespaceId,
        *,
        completed_at: datetime | None = None,
    ) -> None:
        """Update tool invocation outcome after dispatch."""
        self._check_open()
        record_tool_result(
            self._conn,
            invocation_id=result.invocation_id,
            namespace_id=namespace_id,
            tool_result=result,
            completed_at=completed_at,
        )

    def get_tool_invocation(
        self,
        invocation_id: ToolInvocationId,
        namespace_id: NamespaceId,
    ) -> ToolResult | None:
        """Retrieve tool invocation outcome as ToolResult."""
        self._check_open()
        rec = repo_get_tool_invocation(self._conn, invocation_id, namespace_id)
        if rec is None:
            return None
        return _tool_invocation_record_to_result(rec)

    def get_tool_invocation_record(
        self,
        invocation_id: ToolInvocationId,
        namespace_id: NamespaceId,
    ) -> ToolInvocationRecord | None:
        """Retrieve raw ToolInvocationRecord with complete database columns."""
        self._check_open()
        return repo_get_tool_invocation(self._conn, invocation_id, namespace_id)

    def find_invocation_by_idempotency_key(
        self,
        idempotency_key: str,
        namespace_id: NamespaceId,
        tool_name: str | None = None,
    ) -> ToolInvocationRecord | None:
        """Find an existing invocation by unique idempotency key within tenant namespace."""
        self._check_open()
        return find_invocation_by_idempotency_key(
            self._conn,
            namespace_id=namespace_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
        )

    # =========================================================================
    # Checkpoints (StoragePort)
    # =========================================================================

    def save_checkpoint(
        self,
        checkpoint_id: CheckpointId,
        run_id: RunId,
        step_id: StepId,
        branch_id: BranchId,
        namespace_id: NamespaceId,
        state_payload: Mapping[str, Any],
        created_at: datetime,
        parent_checkpoint_id: CheckpointId | None = None,
    ) -> None:
        """Persist durable checkpoint state."""
        self._check_open()
        save_checkpoint(
            self._conn,
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            step_id=step_id,
            branch_id=branch_id,
            namespace_id=namespace_id,
            state_payload=state_payload,
            parent_checkpoint_id=parent_checkpoint_id,
            created_at=created_at,
        )

    def get_checkpoint(
        self,
        checkpoint_id: CheckpointId,
        namespace_id: NamespaceId,
    ) -> Mapping[str, Any] | None:
        """Retrieve checkpoint state payload."""
        self._check_open()
        rec = get_checkpoint(self._conn, checkpoint_id, namespace_id)
        if rec is None:
            return None
        return rec.state_payload()

    def get_checkpoint_record(
        self,
        checkpoint_id: CheckpointId,
        namespace_id: NamespaceId,
        *,
        verify_hash: bool = True,
    ) -> CheckpointRecord | None:
        """Retrieve complete CheckpointRecord snapshot."""
        self._check_open()
        return get_checkpoint(self._conn, checkpoint_id, namespace_id, verify_hash=verify_hash)

    def get_latest_checkpoint(
        self,
        run_id: RunId,
        namespace_id: NamespaceId,
        branch_id: BranchId | None = None,
        *,
        verify_hash: bool = True,
    ) -> CheckpointRecord | None:
        """Retrieve the latest checkpoint record for a run branch."""
        self._check_open()
        return get_latest_checkpoint(
            self._conn,
            namespace_id=namespace_id,
            run_id=run_id,
            branch_id=branch_id or DEFAULT_BRANCH_ID,
            verify_hash=verify_hash,
        )

    # =========================================================================
    # Trace Events (StoragePort)
    # =========================================================================

    def append_trace_event(
        self,
        event_id: EventId,
        run_id: RunId,
        namespace_id: NamespaceId,
        sequence_num: int,
        event_type: str,
        payload: Mapping[str, Any],
        emitted_at: datetime,
        step_id: StepId | None = None,
    ) -> None:
        """Append an immutable trace event to the audit log."""
        self._check_open()
        append_trace_event(
            self._conn,
            event_id=event_id,
            run_id=run_id,
            namespace_id=namespace_id,
            sequence_num=sequence_num,
            event_type=event_type,
            payload=payload,
            emitted_at=emitted_at,
            step_id=step_id,
        )

    def get_trace_event_records(
        self,
        run_id: RunId,
        namespace_id: NamespaceId,
    ) -> tuple[TraceEventRecord, ...]:
        """Retrieve typed TraceEventRecord objects for a run ordered by sequence number."""
        self._check_open()
        return repo_get_trace_events(self._conn, run_id, namespace_id)

    def get_trace_events(
        self,
        run_id: RunId,
        namespace_id: NamespaceId,
    ) -> Sequence[Mapping[str, Any]]:
        """Retrieve trace events as dictionary mappings ordered by sequence number."""
        records = self.get_trace_event_records(run_id, namespace_id)
        return tuple(
            {
                "event_id": str(r.event_id),
                "run_id": str(r.run_id),
                "namespace_id": str(r.namespace_id),
                "sequence_num": r.sequence_num,
                "event_type": r.event_type,
                "payload": r.payload(),
                "state_hash": r.state_hash,
                "emitted_at": r.emitted_at.isoformat(),
                "step_id": str(r.step_id) if r.step_id else None,
            }
            for r in records
        )

    # =========================================================================
    # Atomic Step Commit Protocol
    # =========================================================================

    def commit_step(self, bundle: StepCommitBundle) -> StepCommitOutcome:
        """Execute the atomic 4-phase step commit transaction."""
        self._check_open()
        return commit_step_4phase(self._conn, bundle)

    def commit_step_transaction(
        self,
        *,
        step_result: StepResult,
        run_result: RunResult,
        namespace_id: NamespaceId,
        proposed_memories: Sequence[MemoryRecord] = (),
        lineage_entries: Sequence[LineageEntry] = (),
        checkpoint: CheckpointRecord | None = None,
        trace_events: Sequence[TraceEventRecord | TraceEventInput] = (),
    ) -> StepCommitOutcome:
        """Convenience method to execute 4-phase step commit transaction."""
        self._check_open()
        return commit_step_transaction(
            self._conn,
            step_result=step_result,
            run_result=run_result,
            namespace_id=namespace_id,
            proposed_memories=proposed_memories,
            lineage_entries=lineage_entries,
            checkpoint=checkpoint,
            trace_events=trace_events,
        )

"""Atomic 4-phase step commit protocol implementation.

Guarantees crash-safe, atomic persistence across step execution phases:
- Phase 1: Proposed memory records persistence / update.
- Phase 2: Run execution state and cumulative token updates.
- Phase 3: Step result and tool invocation outcome persistence.
- Phase 4: Memory lineage relationships, checkpoint snapshots, and trace telemetry.

If any phase fails, the entire transaction is rolled back by SQLite, and a
StepCommitError is raised containing detailed compensation diagnostics.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    EventId,
    MemoryId,
    MemoryRecord,
    NamespaceId,
    RunId,
    RunResult,
    StepCommitError,
    StepId,
    StepResult,
)
from memory_aware_agent_harness.storage.checkpoints_repo import (
    CheckpointRecord,
    save_checkpoint,
)
from memory_aware_agent_harness.storage.connection import transaction
from memory_aware_agent_harness.storage.memory_repo import put_memory
from memory_aware_agent_harness.storage.runs_repo import save_run, save_step
from memory_aware_agent_harness.storage.security import (
    PrePersistenceValidator,
    resolve_validator,
)
from memory_aware_agent_harness.storage.trace_repo import (
    TraceEventRecord,
    append_trace_event,
)


@dataclass(frozen=True)
class LineageEntry:
    """Lineage link between a target memory and a source reference."""

    target_memory_id: MemoryId
    source_reference_id: str
    relationship: str = "derived_from"

    def __post_init__(self) -> None:
        if not str(self.target_memory_id).strip():
            raise ValueError("LineageEntry.target_memory_id must not be empty")
        if not str(self.source_reference_id).strip():
            raise ValueError("LineageEntry.source_reference_id must not be empty")
        if not str(self.relationship).strip():
            raise ValueError("LineageEntry.relationship must not be empty")


@dataclass(frozen=True)
class TraceEventInput:
    """Trace event candidate to append during step commit."""

    event_id: EventId
    sequence_num: int
    event_type: str
    payload: Mapping[str, Any]
    emitted_at: datetime | None = None
    step_id: StepId | None = None


@dataclass(frozen=True)
class StepCommitBundle:
    """Bundle of state items to commit atomically in a 4-phase step commit transaction."""

    step_result: StepResult
    run_result: RunResult
    namespace_id: NamespaceId
    proposed_memories: Sequence[MemoryRecord] = ()
    lineage_entries: Sequence[LineageEntry] = ()
    checkpoint: CheckpointRecord | None = None
    trace_events: Sequence[TraceEventRecord | TraceEventInput] = ()


@dataclass(frozen=True)
class StepCommitOutcome:
    """Outcome of a successful step commit transaction."""

    step_id: StepId
    run_id: RunId
    namespace_id: NamespaceId
    memories_committed: int
    lineage_recorded: int
    checkpoint_saved: bool
    trace_events_appended: int
    committed_at: datetime


def commit_step_4phase(
    conn: sqlite3.Connection,
    bundle: StepCommitBundle,
    *,
    validator: PrePersistenceValidator | None = None,
) -> StepCommitOutcome:
    """Execute an atomic 4-phase commit transaction for step execution.

    Phase 1: Persist/update proposed memory records.
    Phase 2: Persist/update run execution state and cumulative totals.
    Phase 3: Persist discrete step result and tool invocation outcome.
    Phase 4: Persist memory lineage, checkpoint snapshot, and trace telemetry.

    Raises:
        StepCommitError: If any phase fails, after full transactional rollback.
    """
    ns_str = str(bundle.namespace_id)
    run_id_str = str(bundle.run_result.run_id)
    step_id_str = str(bundle.step_result.step_id)

    # Pre-flight namespace validations
    if bundle.run_result.namespace_id != bundle.namespace_id:
        raise StepCommitError(
            f"Run result namespace '{bundle.run_result.namespace_id}' does not match "
            f"commit namespace '{bundle.namespace_id}'",
            phase="PREFLIGHT",
            step_id=step_id_str,
            run_id=run_id_str,
            namespace_id=ns_str,
        )

    if bundle.step_result.run_id != bundle.run_result.run_id:
        raise StepCommitError(
            f"Step result run_id '{bundle.step_result.run_id}' does not match "
            f"run result run_id '{bundle.run_result.run_id}'",
            phase="PREFLIGHT",
            step_id=step_id_str,
            run_id=run_id_str,
            namespace_id=ns_str,
        )

    for mem in bundle.proposed_memories:
        if mem.namespace_id != bundle.namespace_id:
            raise StepCommitError(
                f"Proposed memory '{mem.id}' namespace '{mem.namespace_id}' does not match "
                f"commit namespace '{bundle.namespace_id}'",
                phase="PREFLIGHT",
                step_id=step_id_str,
                run_id=run_id_str,
                namespace_id=ns_str,
            )

    if bundle.checkpoint is not None:
        if bundle.checkpoint.namespace_id != bundle.namespace_id:
            raise StepCommitError(
                f"Checkpoint namespace '{bundle.checkpoint.namespace_id}' does not match "
                f"commit namespace '{bundle.namespace_id}'",
                phase="PREFLIGHT",
                step_id=step_id_str,
                run_id=run_id_str,
                namespace_id=ns_str,
            )
        if bundle.checkpoint.run_id != bundle.run_result.run_id:
            raise StepCommitError(
                f"Checkpoint run_id '{bundle.checkpoint.run_id}' does not match "
                f"run result run_id '{bundle.run_result.run_id}'",
                phase="PREFLIGHT",
                step_id=step_id_str,
                run_id=run_id_str,
                namespace_id=ns_str,
            )

    current_phase = "NOT_STARTED"
    try:
        with transaction(conn):
            # Phase 1: Proposed memory records
            current_phase = "PHASE_1_MEMORIES"
            sanitizer = resolve_validator(validator)
            for memory_rec in bundle.proposed_memories:
                clean_rec = sanitizer.sanitize_memory_record(memory_rec)
                put_memory(conn, clean_rec, validator=sanitizer)

            # Phase 2: Run execution state and cumulative totals
            current_phase = "PHASE_2_RUN_STATE"
            save_run(conn, bundle.run_result, validator=sanitizer)

            # Phase 3: Discrete step result
            current_phase = "PHASE_3_STEP_RESULT"
            save_step(conn, bundle.step_result, bundle.namespace_id, validator=sanitizer)

            # Phase 4: Lineage records, checkpoint snapshot, and trace telemetry
            current_phase = "PHASE_4_LINEAGE_AND_TELEMETRY"
            for lineage in bundle.lineage_entries:
                conn.execute(
                    """
                    INSERT INTO memory_lineage (
                        namespace_id, target_memory_id, source_reference_id, relationship
                    ) VALUES (?, ?, ?, ?);
                    """,
                    (
                        ns_str,
                        str(lineage.target_memory_id),
                        sanitizer.sanitize_text(
                            str(lineage.source_reference_id),
                            namespace_id=ns_str,
                            field_name="memory_lineage.source_reference_id",
                            record_id=str(lineage.target_memory_id),
                        ),
                        sanitizer.sanitize_text(
                            lineage.relationship,
                            namespace_id=ns_str,
                            field_name="memory_lineage.relationship",
                            record_id=str(lineage.target_memory_id),
                        ),
                    ),
                )

            if bundle.checkpoint is not None:
                save_checkpoint(
                    conn,
                    checkpoint_id=bundle.checkpoint.checkpoint_id,
                    run_id=bundle.checkpoint.run_id,
                    step_id=bundle.checkpoint.step_id,
                    branch_id=bundle.checkpoint.branch_id,
                    namespace_id=bundle.checkpoint.namespace_id,
                    state_payload=bundle.checkpoint.state_payload(),
                    parent_checkpoint_id=bundle.checkpoint.parent_checkpoint_id,
                    created_at=bundle.checkpoint.created_at,
                    validator=sanitizer,
                )

            for event in bundle.trace_events:
                if isinstance(event, TraceEventRecord):
                    append_trace_event(
                        conn,
                        event_id=event.event_id,
                        run_id=bundle.run_result.run_id,
                        namespace_id=bundle.namespace_id,
                        sequence_num=event.sequence_num,
                        event_type=event.event_type,
                        payload=event.payload(),
                        emitted_at=event.emitted_at,
                        step_id=event.step_id or bundle.step_result.step_id,
                        validator=sanitizer,
                    )
                else:
                    append_trace_event(
                        conn,
                        event_id=event.event_id,
                        run_id=bundle.run_result.run_id,
                        namespace_id=bundle.namespace_id,
                        sequence_num=event.sequence_num,
                        event_type=event.event_type,
                        payload=event.payload,
                        emitted_at=event.emitted_at,
                        step_id=event.step_id or bundle.step_result.step_id,
                        validator=sanitizer,
                    )

    except Exception as exc:
        diagnostics = {
            "failed_phase": current_phase,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "step_id": step_id_str,
            "run_id": run_id_str,
            "namespace_id": ns_str,
            "transaction_status": "ROLLED_BACK",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        raise StepCommitError(
            f"Step commit transaction failed during {current_phase}: {exc}",
            phase=current_phase,
            step_id=step_id_str,
            run_id=run_id_str,
            namespace_id=ns_str,
            details=diagnostics,
        ) from exc

    return StepCommitOutcome(
        step_id=bundle.step_result.step_id,
        run_id=bundle.run_result.run_id,
        namespace_id=bundle.namespace_id,
        memories_committed=len(bundle.proposed_memories),
        lineage_recorded=len(bundle.lineage_entries),
        checkpoint_saved=bundle.checkpoint is not None,
        trace_events_appended=len(bundle.trace_events),
        committed_at=datetime.now(UTC),
    )


def commit_step_transaction(
    conn: sqlite3.Connection,
    *,
    step_result: StepResult,
    run_result: RunResult,
    namespace_id: NamespaceId,
    proposed_memories: Sequence[MemoryRecord] = (),
    lineage_entries: Sequence[LineageEntry] = (),
    checkpoint: CheckpointRecord | None = None,
    trace_events: Sequence[TraceEventRecord | TraceEventInput] = (),
    validator: PrePersistenceValidator | None = None,
) -> StepCommitOutcome:
    """Convenience wrapper for commit_step_4phase."""
    bundle = StepCommitBundle(
        step_result=step_result,
        run_result=run_result,
        namespace_id=namespace_id,
        proposed_memories=proposed_memories,
        lineage_entries=lineage_entries,
        checkpoint=checkpoint,
        trace_events=trace_events,
    )
    return commit_step_4phase(conn, bundle, validator=validator)

"""Checkpoint state rehydration engine with anti-resurrection tombstone safeguards.

Enforces SPEC.md Section 6.5 and ARCHITECTURE.md Section 6.3:
1. Tombstone Reconciliation: Queries active tombstones in the tenant namespace.
2. Purge Filtering: Strips and purges all tombstoned memory IDs from working memory
   and candidate pools embedded in historical checkpoint state payloads.
3. Conflict Refusal: Refuses restoration and raises StateResurrectionConflictError if
   execution cannot safely proceed without the purged information.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from memory_aware_agent_harness.contracts import (
    CheckpointId,
    ClockPort,
    CrossNamespaceAccessError,
    IdGeneratorPort,
    MemoryId,
    NamespaceId,
    RunBudgets,
    RunId,
    StateResurrectionConflictError,
    StoragePort,
)
from memory_aware_agent_harness.runtime.state import LifecycleState
from memory_aware_agent_harness.storage.checkpoints_repo import CheckpointRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RehydratedState:
    """Outcome of reconciling and restoring a checkpoint state."""

    checkpoint_id: CheckpointId
    run_id: RunId
    namespace_id: NamespaceId
    state_payload: Mapping[str, Any]
    purged_memory_ids: frozenset[MemoryId]
    active_tombstones_count: int


def _extract_memory_id(item: Any) -> MemoryId | None:
    """Safely extract MemoryId from an item (dict, object, or string)."""
    if isinstance(item, Mapping):
        raw_id = item.get("id") or item.get("memory_id")
        return MemoryId(str(raw_id)) if raw_id is not None and str(raw_id).strip() else None
    if hasattr(item, "id"):
        raw_id = item.id
        return MemoryId(str(raw_id)) if raw_id is not None and str(raw_id).strip() else None
    if isinstance(item, str) and item.strip():
        return MemoryId(item.strip())
    return None


def rehydrate_checkpoint_state(
    checkpoint: CheckpointRecord | Mapping[str, Any],
    namespace_id: NamespaceId,
    storage: StoragePort,
    *,
    checkpoint_id: CheckpointId | None = None,
    run_id: RunId | None = None,
    critical_memory_ids: Sequence[MemoryId] = (),
    conflict_strategy: Literal["refuse", "purge"] = "purge",
    verify_integrity: bool = True,
) -> RehydratedState:
    """Rehydrate state payload from a checkpoint snapshot with tombstone reconciliation.

    Args:
        checkpoint: CheckpointRecord or raw state payload mapping.
        namespace_id: Tenant namespace identifier.
        storage: StoragePort used to query active tombstones.
        checkpoint_id: Checkpoint identifier when checkpoint is a raw mapping.
        run_id: Run identifier when checkpoint is a raw mapping.
        critical_memory_ids: Memory IDs required for safe continuation. If any are
            tombstoned, StateResurrectionConflictError is raised.
        conflict_strategy: 'purge' strips tombstoned memories (raising only if critical
            memories are purged); 'refuse' raises if ANY memory in checkpoint is tombstoned.
        verify_integrity: When True and checkpoint is a CheckpointRecord, validates state hash.

    Returns:
        RehydratedState containing sanitized state payload and set of purged IDs.

    Raises:
        CrossNamespaceAccessError: If checkpoint belongs to a different namespace.
        StateResurrectionConflictError: If conflict arises under the configured strategy.
    """
    if isinstance(checkpoint, CheckpointRecord):
        if checkpoint.namespace_id != namespace_id:
            raise CrossNamespaceAccessError(
                f"Cannot restore checkpoint '{checkpoint.checkpoint_id}': belongs to namespace "
                f"'{checkpoint.namespace_id}', requested '{namespace_id}'",
                requested_namespace=str(namespace_id),
                target_namespace=str(checkpoint.namespace_id),
                resource_id=str(checkpoint.checkpoint_id),
            )
        if verify_integrity:
            checkpoint.verify_integrity(raise_on_divergence=True)
        raw_payload = checkpoint.state_payload()
        eff_checkpoint_id = checkpoint.checkpoint_id
        eff_run_id = checkpoint.run_id
    else:
        raw_payload = dict(checkpoint)
        eff_checkpoint_id = checkpoint_id or CheckpointId("ckpt_restored")
        eff_run_id = run_id or RunId(str(raw_payload.get("run_id", "run_restored")))

    # Step 1: Tombstone Reconciliation
    active_tombstones = storage.list_tombstoned_memory_ids(namespace_id)
    tombstoned_ids = frozenset(active_tombstones)

    # Step 2: Purge Filtering (Anti-Resurrection)
    sanitized_payload = copy.deepcopy(raw_payload)
    purged_ids: set[MemoryId] = set()

    # Reconcile memory ID lists
    for id_list_key in ("working_memory_ids", "candidate_memory_ids", "memory_ids"):
        if id_list_key in sanitized_payload and isinstance(sanitized_payload[id_list_key], list):
            cleaned_ids: list[str] = []
            for item in sanitized_payload[id_list_key]:
                mid = _extract_memory_id(item)
                if mid is not None and mid in tombstoned_ids:
                    purged_ids.add(mid)
                else:
                    cleaned_ids.append(str(item))
            sanitized_payload[id_list_key] = cleaned_ids

    # Reconcile structured memory objects
    for obj_list_key in ("working_memory", "candidate_pool", "memories", "context_items"):
        if obj_list_key in sanitized_payload and isinstance(sanitized_payload[obj_list_key], list):
            cleaned_objs: list[Any] = []
            for item in sanitized_payload[obj_list_key]:
                mid = _extract_memory_id(item)
                if mid is not None and mid in tombstoned_ids:
                    purged_ids.add(mid)
                else:
                    cleaned_objs.append(item)
            sanitized_payload[obj_list_key] = cleaned_objs

    # Step 3: Conflict Refusal Check
    frozen_purged = frozenset(purged_ids)
    conflicts: list[MemoryId] = []

    if conflict_strategy == "refuse" and frozen_purged:
        conflicts = sorted(frozen_purged)
    elif critical_memory_ids:
        conflicts = [mid for mid in critical_memory_ids if mid in frozen_purged]

    if conflicts:
        raise StateResurrectionConflictError(
            f"State restoration refused: {len(conflicts)} memory record(s) required "
            f"for continuation are tombstoned: {', '.join(str(c) for c in conflicts)}",
            conflicting_memory_ids=conflicts,
            namespace_id=str(namespace_id),
            checkpoint_id=str(eff_checkpoint_id),
        )

    return RehydratedState(
        checkpoint_id=eff_checkpoint_id,
        run_id=eff_run_id,
        namespace_id=namespace_id,
        state_payload=sanitized_payload,
        purged_memory_ids=frozen_purged,
        active_tombstones_count=len(active_tombstones),
    )


class CheckpointRehydrationEngine:
    """Engine coordinating checkpoint restoration and tombstone anti-resurrection safeguards."""

    def __init__(self, storage: StoragePort) -> None:
        self.storage = storage

    def rehydrate(
        self,
        checkpoint: CheckpointRecord | Mapping[str, Any],
        namespace_id: NamespaceId,
        *,
        checkpoint_id: CheckpointId | None = None,
        run_id: RunId | None = None,
        critical_memory_ids: Sequence[MemoryId] = (),
        conflict_strategy: Literal["refuse", "purge"] = "purge",
        verify_integrity: bool = True,
    ) -> RehydratedState:
        """Rehydrate state payload from a checkpoint snapshot with anti-resurrection filtering."""
        return rehydrate_checkpoint_state(
            checkpoint=checkpoint,
            namespace_id=namespace_id,
            storage=self.storage,
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            critical_memory_ids=critical_memory_ids,
            conflict_strategy=conflict_strategy,
            verify_integrity=verify_integrity,
        )

    def rehydrate_lifecycle_state(
        self,
        checkpoint: CheckpointRecord,
        namespace_id: NamespaceId,
        *,
        clock: ClockPort | None = None,
        id_generator: IdGeneratorPort | None = None,
        budgets: RunBudgets | None = None,
        critical_memory_ids: Sequence[MemoryId] = (),
        conflict_strategy: Literal["refuse", "purge"] = "purge",
    ) -> tuple[LifecycleState, RehydratedState]:
        """Reconstruct a functioning LifecycleState from a checkpoint with tombstones reconciled."""
        rehydrated = self.rehydrate(
            checkpoint=checkpoint,
            namespace_id=namespace_id,
            critical_memory_ids=critical_memory_ids,
            conflict_strategy=conflict_strategy,
        )

        state = LifecycleState(
            run_id=checkpoint.run_id,
            namespace_id=namespace_id,
            budgets=budgets,
            clock=clock,
            id_generator=id_generator,
        )

        step_number = rehydrated.state_payload.get("step_number")
        if isinstance(step_number, int) and step_number >= 0:
            state.step_count = step_number

        if "working_memory" in rehydrated.state_payload:
            state.metadata["working_memory"] = rehydrated.state_payload["working_memory"]
        if "working_memory_ids" in rehydrated.state_payload:
            state.metadata["working_memory_ids"] = rehydrated.state_payload["working_memory_ids"]

        state.metadata["rehydrated_from_checkpoint"] = str(checkpoint.checkpoint_id)
        state.metadata["purged_memory_ids"] = [str(m) for m in rehydrated.purged_memory_ids]
        state.last_checkpoint_id = checkpoint.checkpoint_id

        return state, rehydrated

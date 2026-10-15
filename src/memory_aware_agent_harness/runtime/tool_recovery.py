"""Tool idempotency crash recovery and side-effect reconciliation.

Provides safe replay of cached idempotent tool outcomes, detection of in-flight
mutating side-effects across process crashes, and fail-closed prevention of
unverified non-idempotent re-execution.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    DirtyRollbackError,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    SideEffectType,
    StoragePort,
    ToolCallStatus,
    ToolInvocationId,
    ToolReconciliationReport,
    ToolResult,
)
from memory_aware_agent_harness.storage.tools_repo import (
    ToolInvocationRecord,
    compute_argument_fingerprint,
    compute_argument_fingerprint_from_json,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ToolRecoveryEngine",
]


class ToolRecoveryEngine:
    """Reconciles in-flight tool invocations after crashes and enforces idempotency replay."""

    def __init__(self, storage: StoragePort) -> None:
        self.storage = storage

    def reconcile_pending_invocations(
        self,
        namespace_id: NamespaceId,
        run_id: RunId | None = None,
    ) -> ToolReconciliationReport:
        """Scan and reconcile in-flight tool invocations left pending across process crashes.

        - Non-idempotent writes are transitioned to OUTCOME_UNKNOWN and flagged dirty.
        - Idempotent writes and reads are transitioned to OUTCOME_UNKNOWN with retry allowed.
        - Returns a structured ToolReconciliationReport.
        """
        if not hasattr(self.storage, "find_pending_or_unknown_invocations"):
            return ToolReconciliationReport(
                namespace_id=namespace_id,
                reconciled_count=0,
                cached_replayed_count=0,
                marked_unknown_count=0,
                dirty_non_idempotent_count=0,
                run_id=run_id,
            )

        pending_records: tuple[ToolInvocationRecord, ...] = (
            self.storage.find_pending_or_unknown_invocations(namespace_id, run_id=run_id)
        )

        reconciled = 0
        marked_unknown = 0
        dirty_count = 0

        for rec in pending_records:
            if rec.status == ToolCallStatus.PENDING:
                is_mutating_non_idemp = rec.side_effect == SideEffectType.NON_IDEMPOTENT_WRITE
                error_msg = (
                    "Interrupted non-idempotent write during crash; outcome unknown"
                    if is_mutating_non_idemp
                    else "Interrupted invocation during crash; outcome unknown (safe to retry)"
                )
                if hasattr(self.storage, "mark_invocation_unknown"):
                    self.storage.mark_invocation_unknown(
                        rec.invocation_id,
                        namespace_id,
                        error=error_msg,
                    )
                marked_unknown += 1
                reconciled += 1
                if is_mutating_non_idemp:
                    dirty_count += 1
            elif rec.status == ToolCallStatus.OUTCOME_UNKNOWN:
                if rec.side_effect == SideEffectType.NON_IDEMPOTENT_WRITE:
                    dirty_count += 1

        return ToolReconciliationReport(
            namespace_id=namespace_id,
            reconciled_count=reconciled,
            cached_replayed_count=0,
            marked_unknown_count=marked_unknown,
            dirty_non_idempotent_count=dirty_count,
            run_id=run_id,
        )

    def get_replayable_cached_result(
        self,
        idempotency_key: str,
        namespace_id: NamespaceId,
        tool_name: str,
        *,
        arguments: Mapping[str, Any] | None = None,
        argument_fingerprint: str | None = None,
        run_id: RunId | None = None,
        supports_provider_idempotency: bool = False,
        is_non_idempotent: bool = False,
    ) -> ToolResult | None:
        """Check for a reusable cached outcome or block illegal non-idempotent re-execution.

        Args:
            idempotency_key: Unique idempotency key or business operation identifier.
            namespace_id: Tenant namespace.
            tool_name: Registered tool name.
            arguments: Current call input parameters.
            argument_fingerprint: Canonical SHA-256 fingerprint of current arguments.
            run_id: Parent run identifier.
            supports_provider_idempotency: Whether external service safely deduplicates.
            is_non_idempotent: Whether this tool has non-idempotent side effects.

        Returns:
            A cached ToolResult if a prior completed execution exists with matching
            argument fingerprint, or None if fresh execution/retry is permitted.

        Raises:
            DirtyRollbackError: If arguments differ from prior invocation with same key,
            or if a non-idempotent action is in uncertain state without verified provider
            deduplication.
        """
        curr_fingerprint = argument_fingerprint or (
            compute_argument_fingerprint(arguments) if arguments is not None else None
        )

        rec: ToolInvocationRecord | None = None
        if hasattr(self.storage, "find_invocation_by_idempotency_key"):
            rec = self.storage.find_invocation_by_idempotency_key(
                idempotency_key=idempotency_key,
                namespace_id=namespace_id,
                tool_name=tool_name,
            )

        if rec is not None:
            cached_fingerprint = rec.argument_fingerprint or compute_argument_fingerprint_from_json(
                rec.arguments_json
            )

            # Never reuse cached results when argument fingerprints differ!
            if curr_fingerprint is not None and cached_fingerprint != curr_fingerprint:
                raise DirtyRollbackError(
                    f"Idempotency conflict for tool '{tool_name}' with key '{idempotency_key}': "
                    f"argument fingerprint does not match prior invocation. "
                    f"Cached outcome cannot be reused for different parameters.",
                    details={
                        "invocation_id": str(rec.invocation_id),
                        "idempotency_key": idempotency_key,
                        "tool_name": tool_name,
                        "current_fingerprint": curr_fingerprint,
                        "cached_fingerprint": cached_fingerprint,
                        "error_code": "IDEMPOTENCY_ARGUMENT_MISMATCH",
                    },
                )

            # If previously COMPLETED, replay cached result
            if rec.status == ToolCallStatus.COMPLETED:
                # Never reuse cached READ_ONLY results across independent runs!
                if rec.side_effect == SideEffectType.READ_ONLY and (
                    run_id is None or str(rec.run_id) != str(run_id)
                ):
                    return None

                cached_data: Mapping[str, Any] = {}
                if rec.result_json:
                    try:
                        loaded = json.loads(rec.result_json)
                        if isinstance(loaded, Mapping):
                            cached_data = loaded
                    except Exception:
                        cached_data = {"raw": rec.result_json}

                return ToolResult(
                    invocation_id=rec.invocation_id,
                    tool_name=tool_name,
                    status=ToolCallStatus.COMPLETED,
                    side_effect=rec.side_effect,
                    idempotency_key=idempotency_key,
                    result=cached_data,
                    execution_time_ms=0.0,
                )

            # If OUTCOME_UNKNOWN or PENDING on NON_IDEMPOTENT_WRITE:
            if rec.side_effect == SideEffectType.NON_IDEMPOTENT_WRITE and rec.status in (
                ToolCallStatus.OUTCOME_UNKNOWN,
                ToolCallStatus.PENDING,
            ):
                if supports_provider_idempotency:
                    # Verified provider-supported idempotency allows safe retry
                    return None

                raise DirtyRollbackError(
                    f"Automated re-execution blocked: non-idempotent tool '{tool_name}' with key "
                    f"'{idempotency_key}' is in uncertain state '{rec.status.value}'. "
                    f"Manual operator resolution or compensation is required.",
                    details={
                        "invocation_id": str(rec.invocation_id),
                        "idempotency_key": idempotency_key,
                        "tool_name": tool_name,
                        "status": rec.status.value,
                        "side_effect": rec.side_effect.value,
                        "error_code": "PARTIAL_DIRTY_ROLLBACK",
                    },
                )

            # For IDEMPOTENT calls with OUTCOME_UNKNOWN, retry is legally permissible
            return None

        # If not found by key, check for uncertain prior invocation by fingerprint
        # on non-idempotent tool
        if is_non_idempotent and curr_fingerprint is not None:
            finder = getattr(self.storage, "find_uncertain_invocation_by_fingerprint", None)
            prior_uncertain: ToolInvocationRecord | None = None
            if callable(finder):
                prior_uncertain = finder(
                    namespace_id=namespace_id,
                    tool_name=tool_name,
                    argument_fingerprint=curr_fingerprint,
                    run_id=run_id,
                )

            if prior_uncertain is not None:
                if supports_provider_idempotency:
                    return None

                raise DirtyRollbackError(
                    f"Automated re-execution blocked: potentially duplicated non-idempotent "
                    f"tool '{tool_name}' with new call ID has uncertain prior execution "
                    f"(status '{prior_uncertain.status.value}', "
                    f"invocation '{prior_uncertain.invocation_id}'). "
                    "Manual operator resolution or compensation is required.",
                    details={
                        "invocation_id": str(prior_uncertain.invocation_id),
                        "idempotency_key": prior_uncertain.idempotency_key,
                        "tool_name": tool_name,
                        "status": prior_uncertain.status.value,
                        "side_effect": prior_uncertain.side_effect.value,
                        "error_code": "PARTIAL_DIRTY_ROLLBACK",
                    },
                )

        return None

    def resolve_invocation(
        self,
        invocation_id: ToolInvocationId,
        namespace_id: NamespaceId,
        *,
        status: ToolCallStatus,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        completed_at: datetime | None = None,
    ) -> ToolInvocationRecord:
        """Resolve an in-flight or OUTCOME_UNKNOWN tool invocation with an explicit outcome."""
        record_getter = getattr(self.storage, "get_tool_invocation_record", None)
        if not callable(record_getter):
            raise RecordNotFoundError(
                f"Invocation '{invocation_id}' not found for resolution",
                record_id=str(invocation_id),
                namespace_id=str(namespace_id),
                table="tool_invocations",
            )
        existing: ToolInvocationRecord | None = record_getter(invocation_id, namespace_id)
        if existing is None:
            raise RecordNotFoundError(
                f"Invocation '{invocation_id}' not found for resolution",
                record_id=str(invocation_id),
                namespace_id=str(namespace_id),
                table="tool_invocations",
            )

        tool_res = ToolResult(
            invocation_id=invocation_id,
            tool_name=existing.tool_name,
            status=status,
            side_effect=existing.side_effect,
            idempotency_key=existing.idempotency_key or "",
            result=result,
            error=error,
        )
        self.storage.record_tool_result(
            tool_res,
            namespace_id=namespace_id,
            completed_at=completed_at,
        )

        updated: ToolInvocationRecord | None = record_getter(invocation_id, namespace_id)
        if updated is not None:
            return updated

        return existing

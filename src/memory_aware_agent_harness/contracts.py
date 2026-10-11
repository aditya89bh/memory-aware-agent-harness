"""Public typing contracts, data models, protocols, and exceptions for the harness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import (
    Any,
    NewType,
    Protocol,
    runtime_checkable,
)

# --- Strong Type Identifiers (PEP 484 NewType) ---
RunId = NewType("RunId", str)
StepId = NewType("StepId", str)
MemoryId = NewType("MemoryId", str)
NamespaceId = NewType("NamespaceId", str)
CheckpointId = NewType("CheckpointId", str)
BranchId = NewType("BranchId", str)
EventId = NewType("EventId", str)
ToolInvocationId = NewType("ToolInvocationId", str)


# --- Enumerations ---
class RunStatus(StrEnum):
    """Execution lifecycle status of an agent run."""

    INITIALIZED = "initialized"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
    FAILED = "failed"


class MemoryKind(StrEnum):
    """Cognitive behavioral categories for memory records."""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryState(StrEnum):
    """Lifecycle state of a persistent memory record."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DELETED = "deleted"


class CandidateStatus(StrEnum):
    """Validation lifecycle status for a proposed memory candidate."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SideEffectType(StrEnum):
    """Classification of external side effects caused by a tool."""

    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"


class ToolCallStatus(StrEnum):
    """Execution outcome status of a tool invocation."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ModelFinishReason(StrEnum):
    """Reason for model decision completion."""

    TOOL_CALL = "tool_call"
    STOP = "stop"
    LENGTH = "length"
    ERROR = "error"


# --- Immutable Data Records with Contract Validation ---
@dataclass(frozen=True)
class ToolCallRequest:
    """Request from a model to invoke a specific tool."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("ToolCallRequest.call_id must not be empty")
        if not self.tool_name.strip():
            raise ValueError("ToolCallRequest.tool_name must not be empty")


@dataclass(frozen=True)
class ModelDecision:
    """Structured decision output produced by a ModelPort."""

    decision_id: str
    step_id: StepId
    finish_reason: ModelFinishReason
    content: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = field(default_factory=tuple)
    tokens_used: int = 0
    raw_response: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.decision_id.strip():
            raise ValueError("ModelDecision.decision_id must not be empty")
        if not str(self.step_id).strip():
            raise ValueError("ModelDecision.step_id must not be empty")
        if self.tokens_used < 0:
            raise ValueError(f"ModelDecision.tokens_used must be >= 0, got {self.tokens_used}")


@dataclass(frozen=True)
class ToolResult:
    """Execution result returned by a tool invocation."""

    invocation_id: ToolInvocationId
    tool_name: str
    status: ToolCallStatus
    side_effect: SideEffectType
    idempotency_key: str
    result: Mapping[str, Any] | None = None
    error: str | None = None
    execution_time_ms: float = 0.0

    def __post_init__(self) -> None:
        if not str(self.invocation_id).strip():
            raise ValueError("ToolResult.invocation_id must not be empty")
        if not self.tool_name.strip():
            raise ValueError("ToolResult.tool_name must not be empty")
        if not self.idempotency_key.strip():
            raise ValueError("ToolResult.idempotency_key must not be empty")
        if self.execution_time_ms < 0.0:
            raise ValueError(
                f"ToolResult.execution_time_ms must be >= 0.0, got {self.execution_time_ms}"
            )


@dataclass(frozen=True)
class MemoryRecord:
    """Durable memory unit stored in the unified memory store."""

    id: MemoryId
    namespace_id: NamespaceId
    kind: MemoryKind
    content: str
    state: MemoryState
    confidence: float
    salience: float
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    source_references: tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    access_count: int = 0
    version: int = 1

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ValueError("MemoryRecord.id must not be empty")
        if not str(self.namespace_id).strip():
            raise ValueError("MemoryRecord.namespace_id must not be empty")
        if not self.content.strip():
            raise ValueError("MemoryRecord.content must not be empty")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"MemoryRecord.confidence must be between 0.0 and 1.0, got {self.confidence}"
            )
        if not (0.0 <= self.salience <= 1.0):
            raise ValueError(
                f"MemoryRecord.salience must be between 0.0 and 1.0, got {self.salience}"
            )
        if self.created_at.tzinfo is None:
            raise ValueError("MemoryRecord.created_at must be timezone-aware (tzinfo is required)")
        if self.updated_at.tzinfo is None:
            raise ValueError("MemoryRecord.updated_at must be timezone-aware (tzinfo is required)")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("MemoryRecord.expires_at must be timezone-aware when provided")
        if self.access_count < 0:
            raise ValueError(f"MemoryRecord.access_count must be >= 0, got {self.access_count}")
        if self.version < 1:
            raise ValueError(f"MemoryRecord.version must be >= 1, got {self.version}")


@dataclass(frozen=True)
class MemoryCandidate:
    """Extracted memory item proposed for validation and write."""

    candidate_id: str
    namespace_id: NamespaceId
    kind: MemoryKind
    content: str
    proposed_salience: float
    source_step_id: StepId
    rationale: str
    validation_status: CandidateStatus = CandidateStatus.PENDING
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("MemoryCandidate.candidate_id must not be empty")
        if not str(self.namespace_id).strip():
            raise ValueError("MemoryCandidate.namespace_id must not be empty")
        if not self.content.strip():
            raise ValueError("MemoryCandidate.content must not be empty")
        if not str(self.source_step_id).strip():
            raise ValueError("MemoryCandidate.source_step_id must not be empty")
        if not (0.0 <= self.proposed_salience <= 1.0):
            raise ValueError(
                "MemoryCandidate.proposed_salience must be between 0.0 and 1.0, "
                f"got {self.proposed_salience}"
            )


@dataclass(frozen=True)
class ContextItem:
    """A retrieved memory selected for inclusion in the model context."""

    memory_id: MemoryId
    kind: MemoryKind
    content: str
    rank_score: float
    provenance_id: str
    token_cost: int

    def __post_init__(self) -> None:
        if not str(self.memory_id).strip():
            raise ValueError("ContextItem.memory_id must not be empty")
        if not self.content.strip():
            raise ValueError("ContextItem.content must not be empty")
        if self.token_cost < 0:
            raise ValueError(f"ContextItem.token_cost must be >= 0, got {self.token_cost}")


@dataclass(frozen=True)
class ContextManifest:
    """Immutable manifest of compiled prompt context and budget accounting."""

    manifest_id: str
    step_id: StepId
    included_items: tuple[ContextItem, ...]
    excluded_item_ids: Mapping[MemoryId, str]
    total_tokens: int
    budget_limit: int
    compiled_prompt: str

    def __post_init__(self) -> None:
        if not self.manifest_id.strip():
            raise ValueError("ContextManifest.manifest_id must not be empty")
        if not str(self.step_id).strip():
            raise ValueError("ContextManifest.step_id must not be empty")
        if self.total_tokens < 0:
            raise ValueError(f"ContextManifest.total_tokens must be >= 0, got {self.total_tokens}")
        if self.budget_limit < 0:
            raise ValueError(f"ContextManifest.budget_limit must be >= 0, got {self.budget_limit}")


@dataclass(frozen=True)
class StepResult:
    """Outcome of a single lifecycle step execution."""

    step_id: StepId
    run_id: RunId
    step_number: int
    decision: ModelDecision
    tool_results: tuple[ToolResult, ...]
    memories_proposed: int
    memories_committed: int
    tokens_consumed: int
    completed_at: datetime

    def __post_init__(self) -> None:
        if not str(self.step_id).strip():
            raise ValueError("StepResult.step_id must not be empty")
        if not str(self.run_id).strip():
            raise ValueError("StepResult.run_id must not be empty")
        if self.step_number < 0:
            raise ValueError(f"StepResult.step_number must be >= 0, got {self.step_number}")
        if self.memories_proposed < 0 or self.memories_committed < 0:
            raise ValueError("Memory counts must be >= 0")
        if self.tokens_consumed < 0:
            raise ValueError(f"StepResult.tokens_consumed must be >= 0, got {self.tokens_consumed}")
        if self.completed_at.tzinfo is None:
            raise ValueError("StepResult.completed_at must be timezone-aware")


@dataclass(frozen=True)
class RunResult:
    """Terminal outcome of an agent execution run."""

    run_id: RunId
    namespace_id: NamespaceId
    status: RunStatus
    step_count: int
    total_tokens_consumed: int
    final_output: str | None
    trace_id: str
    error: str | None = None

    def __post_init__(self) -> None:
        if not str(self.run_id).strip():
            raise ValueError("RunResult.run_id must not be empty")
        if not str(self.namespace_id).strip():
            raise ValueError("RunResult.namespace_id must not be empty")
        if self.step_count < 0:
            raise ValueError(f"RunResult.step_count must be >= 0, got {self.step_count}")
        if self.total_tokens_consumed < 0:
            raise ValueError(
                f"RunResult.total_tokens_consumed must be >= 0, got {self.total_tokens_consumed}"
            )


# --- Typed Exceptions ---
class HarnessError(Exception):
    """Base class for all harness errors."""


class BudgetExceededError(HarnessError):
    """Raised when step, token, or time budget limit is exceeded."""


class PolicyViolationError(HarnessError):
    """Raised when an operation violates security or safety policy."""


class ToolExecutionError(HarnessError):
    """Raised when tool execution fails in the sandbox."""


class CompensationFailedError(HarnessError):
    """Raised when fallible compensation fails during rollback."""


class ReplayDivergenceError(HarnessError):
    """Raised when deterministic replay deviates from recorded trace."""


class StorageError(HarnessError):
    """Raised on database integrity or persistence failure."""


# --- Core Protocols (Ports) ---
@runtime_checkable
class ModelPort(Protocol):
    """Port for soliciting structured decisions from models or test doubles."""

    def decide(
        self, manifest: ContextManifest, tools_schema: Sequence[Mapping[str, Any]]
    ) -> ModelDecision:
        """Evaluate context manifest and return a structured decision."""
        ...


@runtime_checkable
class MemoryStoreProtocol(Protocol):
    """Port for durable multi-tier memory persistence."""

    def put(self, record: MemoryRecord) -> None:
        """Store or update a memory record."""
        ...

    def get(self, memory_id: MemoryId, namespace_id: NamespaceId) -> MemoryRecord | None:
        """Fetch a single memory record by identifier within a namespace."""
        ...

    def search(
        self,
        namespace_id: NamespaceId,
        query: str,
        kinds: Sequence[MemoryKind],
        limit: int,
    ) -> Sequence[MemoryRecord]:
        """Search memory records by query and category."""
        ...

    def tombstone(self, memory_id: MemoryId, namespace_id: NamespaceId, reason: str) -> None:
        """Logically delete and tombstone a memory record."""
        ...

    def list_active(self, namespace_id: NamespaceId, kind: MemoryKind) -> Sequence[MemoryRecord]:
        """List active memories for a given category."""
        ...


@runtime_checkable
class ToolProtocol(Protocol):
    """Port for executable agent tools."""

    name: str
    description: str
    side_effect: SideEffectType

    def get_schema(self) -> Mapping[str, Any]:
        """Return JSON Schema compatible dictionary of tool arguments."""
        ...

    def execute(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        """Execute tool and return structured observation."""
        ...


__all__ = [
    "BranchId",
    "BudgetExceededError",
    "CandidateStatus",
    "CheckpointId",
    "CompensationFailedError",
    "ContextItem",
    "ContextManifest",
    "EventId",
    "HarnessError",
    "MemoryCandidate",
    "MemoryId",
    "MemoryKind",
    "MemoryRecord",
    "MemoryState",
    "MemoryStoreProtocol",
    "ModelDecision",
    "ModelFinishReason",
    "ModelPort",
    "NamespaceId",
    "PolicyViolationError",
    "ReplayDivergenceError",
    "RunId",
    "RunResult",
    "RunStatus",
    "SideEffectType",
    "StepId",
    "StepResult",
    "StorageError",
    "ToolCallRequest",
    "ToolCallStatus",
    "ToolExecutionError",
    "ToolInvocationId",
    "ToolProtocol",
    "ToolResult",
]

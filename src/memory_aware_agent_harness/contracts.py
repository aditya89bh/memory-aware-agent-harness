"""Public typing contracts, data models, protocols, and exceptions for the harness."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import (
    Any,
    NewType,
    Protocol,
    Self,
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


# --- Normalization and Serialization Helpers ---
def _freeze(val: Any) -> Any:
    """Recursively freeze mutable mappings, sequences, and sets into immutable variants."""
    if isinstance(val, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(_freeze(item) for item in val)
    if isinstance(val, (set, frozenset)):
        return frozenset(_freeze(item) for item in val)
    return val


def _serialize_value(val: Any) -> Any:
    """Recursively serialize complex types into JSON-compatible primitives."""
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, (StrEnum, Enum)):
        return val.value
    if hasattr(val, "to_dict") and callable(val.to_dict):
        return val.to_dict()
    if isinstance(val, Mapping):
        return {str(k): _serialize_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_serialize_value(item) for item in val]
    if isinstance(val, (set, frozenset)):
        return sorted([_serialize_value(item) for item in val])
    return val


def _parse_dt(val: str | datetime) -> datetime:
    """Parse a datetime from an ISO string or validate an existing datetime."""
    if isinstance(val, datetime):
        dt = val
    else:
        # Standardize 'Z' suffix to '+00:00' for broad compatibility
        clean_str = val.replace("Z", "+00:00") if val.endswith("Z") else val
        dt = datetime.fromisoformat(clean_str)

    if dt.tzinfo is None:
        raise ValueError("Datetime must be timezone-aware (tzinfo is required)")
    return dt


class SerializableRecord:
    """Base class for public dataclasses providing canonical serialization and immutability."""

    def to_dict(self) -> dict[str, Any]:
        """Convert record to a JSON-compatible dictionary."""
        out: dict[str, Any] = {}
        for f in fields(self):  # type: ignore[arg-type]
            val = getattr(self, f.name)
            out[f.name] = _serialize_value(val)
        return out

    def to_json(self) -> str:
        """Serialize record to canonical JSON string with sorted keys and compact separators."""
        return json.dumps(self.to_dict(), indent=None, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Construct record instance from a dictionary."""
        raise NotImplementedError("Subclasses must implement from_dict")

    @classmethod
    def from_json(cls, json_str: str) -> Self:
        """Construct record instance from canonical JSON string."""
        return cls.from_dict(json.loads(json_str))


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
class ToolCallRequest(SerializableRecord):
    """Request from a model to invoke a specific tool."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        args = self.arguments if self.arguments is not None else {}
        object.__setattr__(self, "arguments", _freeze(args))

        if not self.call_id.strip():
            raise ValueError("ToolCallRequest.call_id must not be empty")
        if not self.tool_name.strip():
            raise ValueError("ToolCallRequest.tool_name must not be empty")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            call_id=str(data["call_id"]),
            tool_name=str(data["tool_name"]),
            arguments=data.get("arguments", {}),
        )


@dataclass(frozen=True)
class ModelDecision(SerializableRecord):
    """Structured decision output produced by a ModelPort."""

    decision_id: str
    step_id: StepId
    finish_reason: ModelFinishReason
    content: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = field(default_factory=tuple)
    tokens_used: int = 0
    raw_response: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        calls = self.tool_calls if self.tool_calls is not None else ()
        object.__setattr__(self, "tool_calls", tuple(calls))
        if self.raw_response is not None:
            object.__setattr__(self, "raw_response", _freeze(self.raw_response))

        if not self.decision_id.strip():
            raise ValueError("ModelDecision.decision_id must not be empty")
        if not str(self.step_id).strip():
            raise ValueError("ModelDecision.step_id must not be empty")
        if self.tokens_used < 0:
            raise ValueError(f"ModelDecision.tokens_used must be >= 0, got {self.tokens_used}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        raw_calls = data.get("tool_calls", ())
        tool_calls = tuple(
            c if isinstance(c, ToolCallRequest) else ToolCallRequest.from_dict(c) for c in raw_calls
        )
        return cls(
            decision_id=str(data["decision_id"]),
            step_id=StepId(str(data["step_id"])),
            finish_reason=ModelFinishReason(data["finish_reason"]),
            content=data.get("content"),
            tool_calls=tool_calls,
            tokens_used=int(data.get("tokens_used", 0)),
            raw_response=data.get("raw_response"),
        )


@dataclass(frozen=True)
class ToolResult(SerializableRecord):
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
        if self.result is not None:
            object.__setattr__(self, "result", _freeze(self.result))

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

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            invocation_id=ToolInvocationId(str(data["invocation_id"])),
            tool_name=str(data["tool_name"]),
            status=ToolCallStatus(data["status"]),
            side_effect=SideEffectType(data["side_effect"]),
            idempotency_key=str(data["idempotency_key"]),
            result=data.get("result"),
            error=data.get("error"),
            execution_time_ms=float(data.get("execution_time_ms", 0.0)),
        )


@dataclass(frozen=True)
class MemoryRecord(SerializableRecord):
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
        refs = self.source_references if self.source_references is not None else ()
        object.__setattr__(self, "source_references", tuple(refs))
        meta = self.metadata if self.metadata is not None else {}
        object.__setattr__(self, "metadata", _freeze(meta))

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

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        expires_at_raw = data.get("expires_at")
        return cls(
            id=MemoryId(str(data["id"])),
            namespace_id=NamespaceId(str(data["namespace_id"])),
            kind=MemoryKind(data["kind"]),
            content=str(data["content"]),
            state=MemoryState(data["state"]),
            confidence=float(data["confidence"]),
            salience=float(data["salience"]),
            created_at=_parse_dt(data["created_at"]),
            updated_at=_parse_dt(data["updated_at"]),
            expires_at=_parse_dt(expires_at_raw) if expires_at_raw is not None else None,
            source_references=tuple(data.get("source_references", ())),
            metadata=data.get("metadata", {}),
            access_count=int(data.get("access_count", 0)),
            version=int(data.get("version", 1)),
        )


@dataclass(frozen=True)
class MemoryCandidate(SerializableRecord):
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

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        val_status = data.get("validation_status", CandidateStatus.PENDING)
        return cls(
            candidate_id=str(data["candidate_id"]),
            namespace_id=NamespaceId(str(data["namespace_id"])),
            kind=MemoryKind(data["kind"]),
            content=str(data["content"]),
            proposed_salience=float(data["proposed_salience"]),
            source_step_id=StepId(str(data["source_step_id"])),
            rationale=str(data["rationale"]),
            validation_status=CandidateStatus(val_status),
            rejection_reason=data.get("rejection_reason"),
        )


@dataclass(frozen=True)
class ContextItem(SerializableRecord):
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

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            memory_id=MemoryId(str(data["memory_id"])),
            kind=MemoryKind(data["kind"]),
            content=str(data["content"]),
            rank_score=float(data["rank_score"]),
            provenance_id=str(data["provenance_id"]),
            token_cost=int(data["token_cost"]),
        )


@dataclass(frozen=True)
class ContextManifest(SerializableRecord):
    """Immutable manifest of compiled prompt context and budget accounting."""

    manifest_id: str
    step_id: StepId
    included_items: tuple[ContextItem, ...]
    excluded_item_ids: Mapping[MemoryId, str]
    total_tokens: int
    budget_limit: int
    compiled_prompt: str

    def __post_init__(self) -> None:
        inc = self.included_items if self.included_items is not None else ()
        object.__setattr__(self, "included_items", tuple(inc))
        exc = self.excluded_item_ids if self.excluded_item_ids is not None else {}
        object.__setattr__(self, "excluded_item_ids", _freeze(exc))

        if not self.manifest_id.strip():
            raise ValueError("ContextManifest.manifest_id must not be empty")
        if not str(self.step_id).strip():
            raise ValueError("ContextManifest.step_id must not be empty")
        if self.total_tokens < 0:
            raise ValueError(f"ContextManifest.total_tokens must be >= 0, got {self.total_tokens}")
        if self.budget_limit < 0:
            raise ValueError(f"ContextManifest.budget_limit must be >= 0, got {self.budget_limit}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        raw_items = data.get("included_items", ())
        included = tuple(
            item if isinstance(item, ContextItem) else ContextItem.from_dict(item)
            for item in raw_items
        )
        raw_excluded = data.get("excluded_item_ids", {})
        excluded = {MemoryId(str(k)): str(v) for k, v in raw_excluded.items()}
        return cls(
            manifest_id=str(data["manifest_id"]),
            step_id=StepId(str(data["step_id"])),
            included_items=included,
            excluded_item_ids=excluded,
            total_tokens=int(data["total_tokens"]),
            budget_limit=int(data["budget_limit"]),
            compiled_prompt=str(data["compiled_prompt"]),
        )


@dataclass(frozen=True)
class StepResult(SerializableRecord):
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
        tr = self.tool_results if self.tool_results is not None else ()
        object.__setattr__(self, "tool_results", tuple(tr))

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

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        dec_raw = data["decision"]
        decision = (
            dec_raw if isinstance(dec_raw, ModelDecision) else ModelDecision.from_dict(dec_raw)
        )
        raw_tools = data.get("tool_results", ())
        tool_results = tuple(
            t if isinstance(t, ToolResult) else ToolResult.from_dict(t) for t in raw_tools
        )
        return cls(
            step_id=StepId(str(data["step_id"])),
            run_id=RunId(str(data["run_id"])),
            step_number=int(data["step_number"]),
            decision=decision,
            tool_results=tool_results,
            memories_proposed=int(data.get("memories_proposed", 0)),
            memories_committed=int(data.get("memories_committed", 0)),
            tokens_consumed=int(data.get("tokens_consumed", 0)),
            completed_at=_parse_dt(data["completed_at"]),
        )


@dataclass(frozen=True)
class RunResult(SerializableRecord):
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

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            run_id=RunId(str(data["run_id"])),
            namespace_id=NamespaceId(str(data["namespace_id"])),
            status=RunStatus(data["status"]),
            step_count=int(data["step_count"]),
            total_tokens_consumed=int(data["total_tokens_consumed"]),
            final_output=data.get("final_output"),
            trace_id=str(data["trace_id"]),
            error=data.get("error"),
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
    "SerializableRecord",
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

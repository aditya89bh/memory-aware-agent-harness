"""Public typing contracts, data models, protocols, and exceptions for the harness."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import Enum, StrEnum
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


# --- Immutable, Hashable Data Structures ---
class FrozenDict(dict[Any, Any]):
    """Immutable, hashable dictionary that is safe for pickling, deepcopy, and serialization."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if getattr(self, "_initialized", False):
            raise TypeError("FrozenDict is immutable and cannot be re-initialized")
        super().__init__(*args, **kwargs)
        self._initialized: bool = True
        self._hash: int | None = None

    def __setitem__(self, key: Any, value: Any) -> None:
        raise TypeError("FrozenDict is immutable")

    def __delitem__(self, key: Any) -> None:
        raise TypeError("FrozenDict is immutable")

    def clear(self) -> None:
        raise TypeError("FrozenDict is immutable")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("FrozenDict is immutable")

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("FrozenDict is immutable")

    def popitem(self) -> tuple[Any, Any]:
        raise TypeError("FrozenDict is immutable")

    def setdefault(self, key: Any, default: Any = None) -> Any:
        raise TypeError("FrozenDict is immutable")

    def __ior__(self, other: Any) -> Self:
        raise TypeError("FrozenDict is immutable")

    def __or__(self, other: Any) -> FrozenDict:
        return FrozenDict(super().__or__(dict(other)))

    def __ror__(self, other: Any) -> FrozenDict:
        return FrozenDict(super().__ror__(dict(other)))

    def copy(self) -> Self:
        return self

    @classmethod
    def fromkeys(cls, iterable: Any, value: Any = None) -> Self:
        return cls(dict.fromkeys(iterable, value))

    def __hash__(self) -> int:  # type: ignore[override]
        if self._hash is None:
            sorted_items = sorted(self.items(), key=lambda kv: (str(type(kv[0])), str(kv[0])))
            self._hash = hash(tuple(sorted_items))
        return self._hash

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: Any) -> Self:
        return self.__class__(
            {copy.deepcopy(k, memo): copy.deepcopy(v, memo) for k, v in self.items()}
        )

    def __reduce__(self) -> Any:
        return (self.__class__, (dict(self),))

    def __repr__(self) -> str:
        return f"FrozenDict({super().__repr__()})"


# --- Normalization and Serialization Helpers ---
def _freeze(val: Any) -> Any:
    """Recursively validate and freeze metadata and mapping structures into immutable variants."""
    if isinstance(val, Enum):
        raise TypeError(
            f"Unsupported metadata value of type {type(val).__name__}: "
            "metadata values must be JSON-serializable primitives "
            "(str, int, float, bool, None, Mapping, Sequence); "
            "enums should be passed as their primitive string values (.value)"
        )
    if val is None or isinstance(val, str):
        return val
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise ValueError(f"Float value must be finite, got {val}")
        return val
    if isinstance(val, Mapping):
        frozen_data: dict[str, Any] = {}
        for k, v in val.items():
            if not isinstance(k, str) or isinstance(k, Enum):
                raise TypeError(f"Mapping keys must be strings, got {type(k).__name__}")
            frozen_data[k] = _freeze(v)
        return FrozenDict(frozen_data)
    if isinstance(val, (list, tuple)):
        return tuple(_freeze(item) for item in val)
    raise TypeError(
        f"Unsupported metadata value of type {type(val).__name__}: "
        "metadata values must be JSON-serializable primitives "
        "(str, int, float, bool, None, Mapping, Sequence)"
    )


def _serialize_value(val: Any) -> Any:
    """Recursively serialize complex types into JSON-compatible primitives."""
    if val is None or isinstance(val, (str, bool, int)):
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise ValueError(f"Float value must be finite, got {val}")
        return val
    if isinstance(val, datetime):
        if val.tzinfo is None:
            raise ValueError("Datetime must be timezone-aware (tzinfo is required)")
        return val.astimezone(UTC).isoformat()
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
    raise TypeError(f"Unsupported serializable value of type {type(val).__name__}")


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
        return json.dumps(
            self.to_dict(),
            indent=None,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )

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


class StepState(StrEnum):
    """Discrete execution lifecycle phases of a single step."""

    NOT_STARTED = "not_started"
    RETRIEVING = "retrieving"
    COMPILING_CONTEXT = "compiling_context"
    DECIDING = "deciding"
    DISPATCHING_TOOL = "dispatching_tool"
    EXTRACTING_MEMORIES = "extracting_memories"
    COMMITTING_MEMORIES = "committing_memories"
    CHECKPOINTING = "checkpointing"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


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


class MemoryOrigin(StrEnum):
    """Provenance and trust classification for memory records."""

    SYSTEM_PROVIDED = "system_provided"
    USER_EXPLICIT = "user_explicit"
    AGENT_OBSERVATION = "agent_observation"
    DERIVED_CONSOLIDATION = "derived_consolidation"
    EXTERNAL_RETRIEVAL = "external_retrieval"


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


# --- FSM Transition Matrices ---
LEGAL_RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = FrozenDict(
    {
        RunStatus.INITIALIZED: frozenset(
            {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED}
        ),
        RunStatus.RUNNING: frozenset(
            {
                RunStatus.RUNNING,
                RunStatus.PAUSED,
                RunStatus.COMPLETED,
                RunStatus.BUDGET_EXHAUSTED,
                RunStatus.CANCELLED,
                RunStatus.ABORTED,
                RunStatus.FAILED,
            }
        ),
        RunStatus.PAUSED: frozenset(
            {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.ABORTED, RunStatus.FAILED}
        ),
        RunStatus.COMPLETED: frozenset(),
        RunStatus.BUDGET_EXHAUSTED: frozenset(),
        RunStatus.CANCELLED: frozenset(),
        RunStatus.ABORTED: frozenset(),
        RunStatus.FAILED: frozenset(),
    }
)

LEGAL_STEP_TRANSITIONS: Mapping[StepState, frozenset[StepState]] = FrozenDict(
    {
        StepState.NOT_STARTED: frozenset(
            {StepState.RETRIEVING, StepState.FAILED, StepState.ABORTED}
        ),
        StepState.RETRIEVING: frozenset(
            {StepState.COMPILING_CONTEXT, StepState.FAILED, StepState.ABORTED}
        ),
        StepState.COMPILING_CONTEXT: frozenset(
            {StepState.DECIDING, StepState.FAILED, StepState.ABORTED}
        ),
        StepState.DECIDING: frozenset(
            {
                StepState.DISPATCHING_TOOL,
                StepState.EXTRACTING_MEMORIES,
                StepState.COMPLETED,
                StepState.FAILED,
                StepState.ABORTED,
            }
        ),
        StepState.DISPATCHING_TOOL: frozenset(
            {StepState.EXTRACTING_MEMORIES, StepState.FAILED, StepState.ABORTED}
        ),
        StepState.EXTRACTING_MEMORIES: frozenset(
            {
                StepState.COMMITTING_MEMORIES,
                StepState.CHECKPOINTING,
                StepState.COMPLETED,
                StepState.FAILED,
                StepState.ABORTED,
            }
        ),
        StepState.COMMITTING_MEMORIES: frozenset(
            {StepState.CHECKPOINTING, StepState.COMPLETED, StepState.FAILED, StepState.ABORTED}
        ),
        StepState.CHECKPOINTING: frozenset(
            {StepState.COMPLETED, StepState.FAILED, StepState.ABORTED}
        ),
        StepState.COMPLETED: frozenset(),
        StepState.FAILED: frozenset(),
        StepState.ABORTED: frozenset(),
    }
)


# --- Immutable Data Records with Contract Validation ---
@dataclass(frozen=True)
class RunBudgets(SerializableRecord):
    """Configured resource exhaustion limits for an agent execution run."""

    max_steps: int = 50
    max_tokens: int = 100_000
    max_time_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.max_steps <= 0:
            raise ValueError(f"RunBudgets.max_steps must be > 0, got {self.max_steps}")
        if self.max_tokens <= 0:
            raise ValueError(f"RunBudgets.max_tokens must be > 0, got {self.max_tokens}")
        if math.isnan(self.max_time_seconds) or math.isinf(self.max_time_seconds):
            raise ValueError(
                f"RunBudgets.max_time_seconds must be finite, got {self.max_time_seconds}"
            )
        if self.max_time_seconds <= 0.0:
            raise ValueError(
                f"RunBudgets.max_time_seconds must be > 0.0, got {self.max_time_seconds}"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            max_steps=int(data.get("max_steps", 50)),
            max_tokens=int(data.get("max_tokens", 100_000)),
            max_time_seconds=float(data.get("max_time_seconds", 300.0)),
        )


@dataclass(frozen=True)
class TokenUsage(SerializableRecord):
    """Detailed accounting of model token consumption."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        if self.prompt_tokens < 0 or self.completion_tokens < 0 or self.total_tokens < 0:
            raise ValueError("Token counts must be non-negative")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError(
                f"total_tokens ({self.total_tokens}) must equal "
                f"prompt_tokens ({self.prompt_tokens}) + "
                f"completion_tokens ({self.completion_tokens})"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            prompt_tokens=int(data["prompt_tokens"]),
            completion_tokens=int(data["completion_tokens"]),
            total_tokens=int(data["total_tokens"]),
        )


@dataclass(frozen=True)
class FailureDetails(SerializableRecord):
    """Structured diagnostic details for step or run failures."""

    error_type: str
    message: str
    step_id: StepId | None = None
    traceback_summary: str | None = None
    recovery_hint: str | None = None
    error_code: str = "GENERIC_ERROR"

    def __post_init__(self) -> None:
        if not self.error_type.strip():
            raise ValueError("FailureDetails.error_type must not be empty")
        if not self.message.strip():
            raise ValueError("FailureDetails.message must not be empty")
        if not self.error_code.strip():
            raise ValueError("FailureDetails.error_code must not be empty")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        step_id_raw = data.get("step_id")
        return cls(
            error_type=str(data["error_type"]),
            message=str(data["message"]),
            step_id=StepId(str(step_id_raw)) if step_id_raw is not None else None,
            traceback_summary=data.get("traceback_summary"),
            recovery_hint=data.get("recovery_hint"),
            error_code=str(data.get("error_code", "GENERIC_ERROR")),
        )


# --- Typed Exceptions with Structured Fields ---
class HarnessError(Exception):
    """Base class for all harness errors with structured diagnostic fields."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "HARNESS_ERROR",
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.recoverable = recoverable
        if isinstance(details, FailureDetails):
            self.details: Mapping[str, Any] = FrozenDict(details.to_dict())
            self._failure_details: FailureDetails | None = details
        elif isinstance(details, Mapping):
            self.details = _freeze(details)
            self._failure_details = None
        elif details is None:
            self.details = FrozenDict()
            self._failure_details = None
        else:
            raise TypeError(
                f"details must be a Mapping, FailureDetails, or None, got {type(details).__name__}"
            )

    def to_failure_details(self, step_id: StepId | None = None) -> FailureDetails:
        """Convert exception to canonical FailureDetails."""
        if self._failure_details is not None:
            if step_id is not None and self._failure_details.step_id is None:
                return FailureDetails(
                    error_type=self._failure_details.error_type,
                    message=self._failure_details.message,
                    step_id=step_id,
                    traceback_summary=self._failure_details.traceback_summary,
                    recovery_hint=self._failure_details.recovery_hint,
                    error_code=self._failure_details.error_code,
                )
            return self._failure_details
        hint = self.details.get("recovery_hint")
        recovery_hint = (
            str(hint)
            if hint is not None
            else ("Retry operation" if self.recoverable else "Abort execution")
        )
        sid = self.details.get("step_id")
        effective_step_id = (
            step_id if step_id is not None else (StepId(str(sid)) if sid is not None else None)
        )
        return FailureDetails(
            error_type=type(self).__name__,
            message=self.message,
            step_id=effective_step_id,
            error_code=self.error_code,
            recovery_hint=recovery_hint,
        )


class BudgetExceededError(HarnessError):
    """Raised when step, token, or time budget limit is exceeded."""

    def __init__(
        self,
        message: str,
        *,
        budget_type: str | None = None,
        limit: int | float | None = None,
        observed: int | float | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if budget_type is not None:
            d["budget_type"] = budget_type
        if limit is not None:
            d["limit"] = limit
        if observed is not None:
            d["observed"] = observed
        super().__init__(
            message,
            error_code="BUDGET_EXCEEDED",
            details=d,
            recoverable=recoverable,
        )
        self.budget_type = budget_type
        self.limit = limit
        self.observed = observed


class PolicyViolationError(HarnessError):
    """Raised when an operation violates security or safety policy."""

    def __init__(
        self,
        message: str,
        *,
        policy_name: str | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if policy_name is not None:
            d["policy_name"] = policy_name
        super().__init__(
            message,
            error_code="POLICY_VIOLATION",
            details=d,
            recoverable=recoverable,
        )
        self.policy_name = policy_name


class ToolExecutionError(HarnessError):
    """Raised when tool execution fails in the sandbox."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        invocation_id: ToolInvocationId | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if tool_name is not None:
            d["tool_name"] = tool_name
        if invocation_id is not None:
            d["invocation_id"] = str(invocation_id)
        super().__init__(
            message,
            error_code="TOOL_EXECUTION_ERROR",
            details=d,
            recoverable=recoverable,
        )
        self.tool_name = tool_name
        self.invocation_id = invocation_id


class CompensationFailedError(HarnessError):
    """Raised when fallible compensation fails during rollback."""

    def __init__(
        self,
        message: str,
        *,
        step_id: StepId | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if step_id is not None:
            d["step_id"] = str(step_id)
        super().__init__(
            message,
            error_code="COMPENSATION_FAILED",
            details=d,
            recoverable=recoverable,
        )
        self.step_id = step_id


class ReplayDivergenceError(HarnessError):
    """Raised when deterministic replay deviates from recorded trace."""

    def __init__(
        self,
        message: str,
        *,
        expected_hash: str | None = None,
        actual_hash: str | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if expected_hash is not None:
            d["expected_hash"] = expected_hash
        if actual_hash is not None:
            d["actual_hash"] = actual_hash
        super().__init__(
            message,
            error_code="REPLAY_DIVERGENCE",
            details=d,
            recoverable=recoverable,
        )
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash


class StorageError(HarnessError):
    """Raised on database integrity or persistence failure."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if operation is not None:
            d["operation"] = operation
        super().__init__(
            message,
            error_code="STORAGE_ERROR",
            details=d,
            recoverable=recoverable,
        )
        self.operation = operation


class ExecutionCancelledError(HarnessError):
    """Raised when execution has been explicitly cancelled."""

    def __init__(
        self,
        message: str = "Execution was cancelled",
        *,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        super().__init__(
            message,
            error_code="EXECUTION_CANCELLED",
            details=details,
            recoverable=recoverable,
        )


class DeadlineExceededError(HarnessError):
    """Raised when an operation exceeds its configured deadline."""

    def __init__(
        self,
        message: str = "Deadline exceeded",
        *,
        deadline: datetime | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if deadline is not None:
            d["deadline"] = deadline.isoformat()
        super().__init__(
            message,
            error_code="DEADLINE_EXCEEDED",
            details=d,
            recoverable=recoverable,
        )
        self.deadline = deadline


class InvalidStateTransitionError(HarnessError):
    """Raised when an FSM transition is attempted that violates the legal transition matrix."""

    def __init__(
        self,
        message: str,
        *,
        from_state: str | None = None,
        to_state: str | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if from_state is not None:
            d["from_state"] = from_state
        if to_state is not None:
            d["to_state"] = to_state
        super().__init__(
            message,
            error_code="INVALID_STATE_TRANSITION",
            details=d,
            recoverable=recoverable,
        )
        self.from_state = from_state
        self.to_state = to_state


@dataclass(frozen=True)
class ModelCallContext(SerializableRecord):
    """Runtime context provided to a ModelPort invocation."""

    run_id: RunId
    step_id: StepId
    namespace_id: NamespaceId
    budget_remaining_tokens: int
    deadline: datetime | None = None
    is_cancelled: bool = False

    def __post_init__(self) -> None:
        if not str(self.run_id).strip():
            raise ValueError("ModelCallContext.run_id must not be empty")
        if not str(self.step_id).strip():
            raise ValueError("ModelCallContext.step_id must not be empty")
        if not str(self.namespace_id).strip():
            raise ValueError("ModelCallContext.namespace_id must not be empty")
        if self.budget_remaining_tokens < 0:
            raise ValueError(
                "ModelCallContext.budget_remaining_tokens must be >= 0, "
                f"got {self.budget_remaining_tokens}"
            )
        if self.deadline is not None and self.deadline.tzinfo is None:
            raise ValueError("ModelCallContext.deadline must be timezone-aware when provided")

    def check_cancellation(self) -> None:
        """Raise ExecutionCancelledError if execution has been marked cancelled."""
        if self.is_cancelled:
            raise ExecutionCancelledError(
                f"Execution run '{self.run_id}' step '{self.step_id}' was cancelled",
                details={
                    "run_id": str(self.run_id),
                    "step_id": str(self.step_id),
                    "error_code": "EXECUTION_CANCELLED",
                    "recovery_hint": "Abort or resume after unpausing",
                },
            )

    def is_expired(self, now: datetime) -> bool:
        """Check if call deadline has elapsed."""
        if self.deadline is None:
            return False
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware (tzinfo is required)")
        return now >= self.deadline

    def check_deadline(self, now: datetime) -> None:
        """Raise DeadlineExceededError if call deadline has elapsed."""
        if self.is_expired(now):
            raise DeadlineExceededError(
                f"Deadline exceeded for run '{self.run_id}' step '{self.step_id}'",
                deadline=self.deadline,
                details={
                    "run_id": str(self.run_id),
                    "step_id": str(self.step_id),
                    "deadline": self.deadline.isoformat() if self.deadline else None,
                    "error_code": "DEADLINE_EXCEEDED",
                    "recovery_hint": "Increase timeout or optimize prompt",
                },
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        deadline_raw = data.get("deadline")
        return cls(
            run_id=RunId(str(data["run_id"])),
            step_id=StepId(str(data["step_id"])),
            namespace_id=NamespaceId(str(data["namespace_id"])),
            budget_remaining_tokens=int(data["budget_remaining_tokens"]),
            deadline=_parse_dt(deadline_raw) if deadline_raw is not None else None,
            is_cancelled=bool(data.get("is_cancelled", False)),
        )


@dataclass(frozen=True)
class ToolCallContext(SerializableRecord):
    """Runtime context provided to a tool execution."""

    run_id: RunId
    step_id: StepId
    namespace_id: NamespaceId
    invocation_id: ToolInvocationId
    idempotency_key: str
    timeout_seconds: float = 30.0
    deadline: datetime | None = None
    is_cancelled: bool = False

    def __post_init__(self) -> None:
        if not str(self.run_id).strip():
            raise ValueError("ToolCallContext.run_id must not be empty")
        if not str(self.step_id).strip():
            raise ValueError("ToolCallContext.step_id must not be empty")
        if not str(self.namespace_id).strip():
            raise ValueError("ToolCallContext.namespace_id must not be empty")
        if not str(self.invocation_id).strip():
            raise ValueError("ToolCallContext.invocation_id must not be empty")
        if not self.idempotency_key.strip():
            raise ValueError("ToolCallContext.idempotency_key must not be empty")
        if math.isnan(self.timeout_seconds) or math.isinf(self.timeout_seconds):
            raise ValueError(
                f"ToolCallContext.timeout_seconds must be finite, got {self.timeout_seconds}"
            )
        if self.timeout_seconds <= 0.0:
            raise ValueError(
                f"ToolCallContext.timeout_seconds must be > 0.0, got {self.timeout_seconds}"
            )
        if self.deadline is not None and self.deadline.tzinfo is None:
            raise ValueError("ToolCallContext.deadline must be timezone-aware when provided")

    def check_cancellation(self) -> None:
        """Raise ExecutionCancelledError if execution has been marked cancelled."""
        if self.is_cancelled:
            raise ExecutionCancelledError(
                f"Tool invocation '{self.invocation_id}' was cancelled",
                details={
                    "run_id": str(self.run_id),
                    "step_id": str(self.step_id),
                    "invocation_id": str(self.invocation_id),
                    "error_code": "EXECUTION_CANCELLED",
                    "recovery_hint": "Abort or re-dispatch with fresh intent",
                },
            )

    def is_expired(self, now: datetime) -> bool:
        """Check if tool call deadline has elapsed."""
        if self.deadline is None:
            return False
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware (tzinfo is required)")
        return now >= self.deadline

    def check_deadline(self, now: datetime) -> None:
        """Raise DeadlineExceededError if tool call deadline has elapsed."""
        if self.is_expired(now):
            raise DeadlineExceededError(
                f"Deadline exceeded for tool invocation '{self.invocation_id}'",
                deadline=self.deadline,
                details={
                    "run_id": str(self.run_id),
                    "step_id": str(self.step_id),
                    "invocation_id": str(self.invocation_id),
                    "deadline": self.deadline.isoformat() if self.deadline else None,
                    "error_code": "DEADLINE_EXCEEDED",
                    "recovery_hint": "Increase timeout_seconds or inspect external service latency",
                },
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        deadline_raw = data.get("deadline")
        return cls(
            run_id=RunId(str(data["run_id"])),
            step_id=StepId(str(data["step_id"])),
            namespace_id=NamespaceId(str(data["namespace_id"])),
            invocation_id=ToolInvocationId(str(data["invocation_id"])),
            idempotency_key=str(data["idempotency_key"]),
            timeout_seconds=float(data.get("timeout_seconds", 30.0)),
            deadline=_parse_dt(deadline_raw) if deadline_raw is not None else None,
            is_cancelled=bool(data.get("is_cancelled", False)),
        )


@dataclass(frozen=True)
class ToolCallRequest(SerializableRecord):
    """Request from a model to invoke a specific tool."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=FrozenDict)

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
    token_usage: TokenUsage | None = None
    failure_details: FailureDetails | None = None
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
        if self.token_usage is not None and self.tokens_used == 0:
            object.__setattr__(self, "tokens_used", self.token_usage.total_tokens)
        elif self.token_usage is not None and self.tokens_used != self.token_usage.total_tokens:
            raise ValueError(
                f"ModelDecision.tokens_used ({self.tokens_used}) does not match "
                f"token_usage.total_tokens ({self.token_usage.total_tokens})"
            )
        if self.tokens_used < 0:
            raise ValueError(f"ModelDecision.tokens_used must be >= 0, got {self.tokens_used}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        raw_calls = data.get("tool_calls", ())
        tool_calls = tuple(
            c if isinstance(c, ToolCallRequest) else ToolCallRequest.from_dict(c) for c in raw_calls
        )
        tu_raw = data.get("token_usage")
        token_usage = (
            tu_raw
            if isinstance(tu_raw, TokenUsage) or tu_raw is None
            else TokenUsage.from_dict(tu_raw)
        )
        fd_raw = data.get("failure_details")
        failure_details = (
            fd_raw
            if isinstance(fd_raw, FailureDetails) or fd_raw is None
            else FailureDetails.from_dict(fd_raw)
        )
        return cls(
            decision_id=str(data["decision_id"]),
            step_id=StepId(str(data["step_id"])),
            finish_reason=ModelFinishReason(data["finish_reason"]),
            content=data.get("content"),
            tool_calls=tool_calls,
            tokens_used=int(data.get("tokens_used", 0)),
            token_usage=token_usage,
            failure_details=failure_details,
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
    failure_details: FailureDetails | None = None
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
        if math.isnan(self.execution_time_ms) or math.isinf(self.execution_time_ms):
            raise ValueError(
                f"ToolResult.execution_time_ms must be finite, got {self.execution_time_ms}"
            )
        if self.execution_time_ms < 0.0:
            raise ValueError(
                f"ToolResult.execution_time_ms must be >= 0.0, got {self.execution_time_ms}"
            )
        if self.failure_details is not None and self.error is None:
            object.__setattr__(self, "error", self.failure_details.message)
        elif (
            self.error is not None
            and self.failure_details is None
            and self.status in (ToolCallStatus.FAILED, ToolCallStatus.OUTCOME_UNKNOWN)
        ):
            object.__setattr__(
                self,
                "failure_details",
                FailureDetails(
                    error_type="ToolExecutionError",
                    message=self.error,
                    error_code="TOOL_ERROR",
                ),
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        fd_raw = data.get("failure_details")
        failure_details = (
            fd_raw
            if isinstance(fd_raw, FailureDetails) or fd_raw is None
            else FailureDetails.from_dict(fd_raw)
        )
        return cls(
            invocation_id=ToolInvocationId(str(data["invocation_id"])),
            tool_name=str(data["tool_name"]),
            status=ToolCallStatus(data["status"]),
            side_effect=SideEffectType(data["side_effect"]),
            idempotency_key=str(data["idempotency_key"]),
            result=data.get("result"),
            error=data.get("error"),
            failure_details=failure_details,
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
    metadata: Mapping[str, Any] = field(default_factory=FrozenDict)
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
        if math.isnan(self.confidence) or math.isinf(self.confidence):
            raise ValueError(f"MemoryRecord.confidence must be finite, got {self.confidence}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"MemoryRecord.confidence must be between 0.0 and 1.0, got {self.confidence}"
            )
        if math.isnan(self.salience) or math.isinf(self.salience):
            raise ValueError(f"MemoryRecord.salience must be finite, got {self.salience}")
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
        if math.isnan(self.proposed_salience) or math.isinf(self.proposed_salience):
            raise ValueError(
                f"MemoryCandidate.proposed_salience must be finite, got {self.proposed_salience}"
            )
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
        if math.isnan(self.rank_score) or math.isinf(self.rank_score):
            raise ValueError(f"ContextItem.rank_score must be finite, got {self.rank_score}")
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
    token_usage: TokenUsage | None = None
    failure_details: FailureDetails | None = None

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
        if self.token_usage is not None and self.tokens_consumed == 0:
            object.__setattr__(self, "tokens_consumed", self.token_usage.total_tokens)
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
        tu_raw = data.get("token_usage")
        token_usage = (
            tu_raw
            if isinstance(tu_raw, TokenUsage) or tu_raw is None
            else TokenUsage.from_dict(tu_raw)
        )
        fd_raw = data.get("failure_details")
        failure_details = (
            fd_raw
            if isinstance(fd_raw, FailureDetails) or fd_raw is None
            else FailureDetails.from_dict(fd_raw)
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
            token_usage=token_usage,
            failure_details=failure_details,
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
    token_usage: TokenUsage | None = None
    failure_details: FailureDetails | None = None

    def __post_init__(self) -> None:
        if not str(self.run_id).strip():
            raise ValueError("RunResult.run_id must not be empty")
        if not str(self.namespace_id).strip():
            raise ValueError("RunResult.namespace_id must not be empty")
        if self.step_count < 0:
            raise ValueError(f"RunResult.step_count must be >= 0, got {self.step_count}")
        if self.token_usage is not None and self.total_tokens_consumed == 0:
            object.__setattr__(self, "total_tokens_consumed", self.token_usage.total_tokens)
        if self.total_tokens_consumed < 0:
            raise ValueError(
                f"RunResult.total_tokens_consumed must be >= 0, got {self.total_tokens_consumed}"
            )
        if self.failure_details is not None and self.error is None:
            object.__setattr__(self, "error", self.failure_details.message)
        elif (
            self.error is not None
            and self.failure_details is None
            and self.status == RunStatus.FAILED
        ):
            object.__setattr__(
                self,
                "failure_details",
                FailureDetails(
                    error_type="HarnessError",
                    message=self.error,
                    error_code="RUN_FAILED",
                ),
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        tu_raw = data.get("token_usage")
        token_usage = (
            tu_raw
            if isinstance(tu_raw, TokenUsage) or tu_raw is None
            else TokenUsage.from_dict(tu_raw)
        )
        fd_raw = data.get("failure_details")
        failure_details = (
            fd_raw
            if isinstance(fd_raw, FailureDetails) or fd_raw is None
            else FailureDetails.from_dict(fd_raw)
        )
        return cls(
            run_id=RunId(str(data["run_id"])),
            namespace_id=NamespaceId(str(data["namespace_id"])),
            status=RunStatus(data["status"]),
            step_count=int(data["step_count"]),
            total_tokens_consumed=int(data.get("total_tokens_consumed", 0)),
            final_output=data.get("final_output"),
            trace_id=str(data["trace_id"]),
            error=data.get("error"),
            token_usage=token_usage,
            failure_details=failure_details,
        )


# --- Core Protocols (Ports) ---
@runtime_checkable
class ClockPort(Protocol):
    """Port for time provision to enable deterministic time traveling."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""
        ...


@runtime_checkable
class IdGeneratorPort(Protocol):
    """Port for identifier generation to enable deterministic replay."""

    def generate_id(self, prefix: str = "") -> str:
        """Generate a unique string identifier with optional domain prefix."""
        ...


@runtime_checkable
class ModelPort(Protocol):
    """Port for soliciting structured decisions from models or test doubles."""

    def decide(
        self,
        manifest: ContextManifest,
        tools_schema: Sequence[Mapping[str, Any]],
        context: ModelCallContext,
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

    def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolCallContext,
    ) -> Mapping[str, Any]:
        """Execute tool and return structured observation."""
        ...


__all__ = [
    "LEGAL_RUN_TRANSITIONS",
    "LEGAL_STEP_TRANSITIONS",
    "BranchId",
    "BudgetExceededError",
    "CandidateStatus",
    "CheckpointId",
    "ClockPort",
    "CompensationFailedError",
    "ContextItem",
    "ContextManifest",
    "DeadlineExceededError",
    "EventId",
    "ExecutionCancelledError",
    "FailureDetails",
    "FrozenDict",
    "HarnessError",
    "IdGeneratorPort",
    "InvalidStateTransitionError",
    "MemoryCandidate",
    "MemoryId",
    "MemoryKind",
    "MemoryOrigin",
    "MemoryRecord",
    "MemoryState",
    "MemoryStoreProtocol",
    "ModelCallContext",
    "ModelDecision",
    "ModelFinishReason",
    "ModelPort",
    "NamespaceId",
    "PolicyViolationError",
    "ReplayDivergenceError",
    "RunBudgets",
    "RunId",
    "RunResult",
    "RunStatus",
    "SerializableRecord",
    "SideEffectType",
    "StepId",
    "StepResult",
    "StepState",
    "StorageError",
    "TokenUsage",
    "ToolCallContext",
    "ToolCallRequest",
    "ToolCallStatus",
    "ToolExecutionError",
    "ToolInvocationId",
    "ToolProtocol",
    "ToolResult",
]

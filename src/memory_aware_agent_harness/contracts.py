"""Public typing contracts, data models, protocols, and exceptions for the harness."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import (
    Any,
    NewType,
    Protocol,
    Self,
    TypeVar,
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
class FrozenDict(Mapping[Any, Any]):
    """Immutable, hashable mapping that is safe for pickling, deepcopy, and serialization."""

    _data: MappingProxyType[Any, Any]
    _initialized: bool
    _hash: int | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if getattr(self, "_initialized", False):
            raise TypeError("FrozenDict is immutable and cannot be re-initialized")
        raw_dict: dict[Any, Any] = dict(*args, **kwargs)
        object.__setattr__(self, "_data", MappingProxyType(raw_dict))
        object.__setattr__(self, "_initialized", True)
        object.__setattr__(self, "_hash", None)

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("FrozenDict is immutable and attributes cannot be modified")

    def __delattr__(self, name: str) -> None:
        raise TypeError("FrozenDict is immutable and attributes cannot be deleted")

    def __getitem__(self, key: Any) -> Any:
        return self._data[key]

    def __iter__(self) -> Any:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def get(self, key: Any, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self) -> Any:
        return self._data.keys()

    def values(self) -> Any:
        return self._data.values()

    def items(self) -> Any:
        return self._data.items()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return bool(self._data == dict(other))
        return False

    def __ne__(self, other: object) -> bool:
        return not (self == other)

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
        if not isinstance(other, Mapping):
            return NotImplemented
        combined = dict(self._data)
        combined.update(other)
        return FrozenDict(combined)

    def __ror__(self, other: Any) -> FrozenDict:
        if not isinstance(other, Mapping):
            return NotImplemented
        combined = dict(other)
        combined.update(self._data)
        return FrozenDict(combined)

    def copy(self) -> Self:
        return self

    @classmethod
    def fromkeys(cls, iterable: Any, value: Any = None) -> Self:
        return cls(dict.fromkeys(iterable, value))

    def __hash__(self) -> int:
        cached = getattr(self, "_hash", None)
        if cached is None:
            sorted_items = sorted(self._data.items(), key=lambda kv: (str(type(kv[0])), str(kv[0])))
            cached = hash(tuple(sorted_items))
            object.__setattr__(self, "_hash", cached)
        return cached

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: Any) -> Self:
        return self.__class__(
            {copy.deepcopy(k, memo): copy.deepcopy(v, memo) for k, v in self._data.items()}
        )

    def __reduce__(self) -> Any:
        return (self.__class__, (dict(self._data),))

    def __repr__(self) -> str:
        return f"FrozenDict({dict(self._data)!r})"


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
    if isinstance(val, list | tuple):
        return tuple(_freeze(item) for item in val)
    raise TypeError(
        f"Unsupported metadata value of type {type(val).__name__}: "
        "metadata values must be JSON-serializable primitives "
        "(str, int, float, bool, None, Mapping, Sequence)"
    )


def _serialize_value(val: Any) -> Any:
    """Recursively serialize complex types into JSON-compatible primitives."""
    if val is None or isinstance(val, str | bool | int):
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise ValueError(f"Float value must be finite, got {val}")
        return val
    if isinstance(val, datetime):
        if val.tzinfo is None:
            raise ValueError("Datetime must be timezone-aware (tzinfo is required)")
        return val.astimezone(UTC).isoformat()
    if isinstance(val, StrEnum | Enum):
        return val.value
    if isinstance(val, CancellationToken):
        return {"is_cancelled": val.is_cancelled, "reason": val.reason}
    if hasattr(val, "to_dict") and callable(val.to_dict):
        return val.to_dict()
    if isinstance(val, Mapping):
        return {str(k): _serialize_value(v) for k, v in val.items()}
    if isinstance(val, list | tuple):
        return [_serialize_value(item) for item in val]
    if isinstance(val, set | frozenset):
        return sorted([_serialize_value(item) for item in val])
    raise TypeError(f"Unsupported serializable value of type {type(val).__name__}")


def to_canonical_json(val: Any) -> str:
    """Serialize arbitrary data structures into canonical JSON at storage boundaries.

    Supports nested dictionaries, FrozenDict, lists, tuples, sets, and JSON primitives.
    Rejects unsupported types and non-finite floats with SerializationError.
    """
    try:
        primitive = _serialize_value(val)
        return json.dumps(
            primitive,
            indent=None,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
            ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            f"Failed to serialize value to canonical JSON: {exc}",
            operation="canonical_json_serialization",
            details={"error": str(exc), "type": type(val).__name__},
        ) from exc


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


_E = TypeVar("_E", bound=Enum)


def _coerce_enum(enum_cls: type[_E], value: Any, field_name: str) -> _E:
    """Validate or safely coerce an enum value without lossy conversion."""
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError as err:
            valid_vals = [e.value for e in enum_cls]
            raise ValueError(
                f"{field_name} must be a valid {enum_cls.__name__} "
                f"(one of {valid_vals}), got {value!r}"
            ) from err
    raise TypeError(
        f"{field_name} must be an instance of {enum_cls.__name__} or str, "
        f"got {type(value).__name__}"
    )


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
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int):
            raise TypeError(
                f"RunBudgets.max_steps must be int, got {type(self.max_steps).__name__}"
            )
        if self.max_steps <= 0:
            raise ValueError(f"RunBudgets.max_steps must be > 0, got {self.max_steps}")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise TypeError(
                f"RunBudgets.max_tokens must be int, got {type(self.max_tokens).__name__}"
            )
        if self.max_tokens <= 0:
            raise ValueError(f"RunBudgets.max_tokens must be > 0, got {self.max_tokens}")
        if isinstance(self.max_time_seconds, bool) or not isinstance(
            self.max_time_seconds, int | float
        ):
            raise TypeError(
                f"RunBudgets.max_time_seconds must be float, "
                f"got {type(self.max_time_seconds).__name__}"
            )
        if math.isnan(self.max_time_seconds) or math.isinf(self.max_time_seconds):
            raise ValueError(
                f"RunBudgets.max_time_seconds must be finite, got {self.max_time_seconds}"
            )
        if self.max_time_seconds <= 0.0:
            raise ValueError(
                f"RunBudgets.max_time_seconds must be > 0.0, got {self.max_time_seconds}"
            )

    def check_step_budget(self, steps_taken: int) -> None:
        """Raise BudgetExceededError if steps_taken exceeds max_steps."""
        if steps_taken >= self.max_steps:
            raise BudgetExceededError(
                f"Step budget exceeded: executed {steps_taken} steps, max is {self.max_steps}",
                budget_type="steps",
                limit=self.max_steps,
                observed=steps_taken,
                details={
                    "error_code": "BUDGET_EXCEEDED",
                    "budget_type": "steps",
                    "limit": self.max_steps,
                    "observed": steps_taken,
                    "recovery_hint": (
                        "Increase RunBudgets.max_steps or decompose task into smaller sub-tasks"
                    ),
                },
            )

    def check_token_budget(self, tokens_used: int) -> None:
        """Raise BudgetExceededError if tokens_used exceeds max_tokens."""
        if tokens_used > self.max_tokens:
            raise BudgetExceededError(
                f"Token budget exceeded: consumed {tokens_used} tokens, max is {self.max_tokens}",
                budget_type="tokens",
                limit=self.max_tokens,
                observed=tokens_used,
                details={
                    "error_code": "BUDGET_EXCEEDED",
                    "budget_type": "tokens",
                    "limit": self.max_tokens,
                    "observed": tokens_used,
                    "recovery_hint": (
                        "Increase RunBudgets.max_tokens or reduce context window size"
                    ),
                },
            )

    def check_time_budget(self, elapsed_seconds: float) -> None:
        """Raise BudgetExceededError if elapsed_seconds exceeds max_time_seconds."""
        if elapsed_seconds > self.max_time_seconds:
            raise BudgetExceededError(
                f"Time budget exceeded: elapsed {elapsed_seconds:.2f}s, "
                f"max is {self.max_time_seconds:.2f}s",
                budget_type="time",
                limit=self.max_time_seconds,
                observed=elapsed_seconds,
                details={
                    "error_code": "BUDGET_EXCEEDED",
                    "budget_type": "time",
                    "limit": self.max_time_seconds,
                    "observed": elapsed_seconds,
                    "recovery_hint": (
                        "Increase RunBudgets.max_time_seconds or optimize tool execution latency"
                    ),
                },
            )

    def remaining_tokens(self, tokens_used: int) -> int:
        """Return remaining tokens before exceeding budget ceiling."""
        return max(0, self.max_tokens - tokens_used)

    def remaining_steps(self, steps_taken: int) -> int:
        """Return remaining steps before reaching step budget limit."""
        return max(0, self.max_steps - steps_taken)

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
        if (
            isinstance(self.prompt_tokens, bool)
            or not isinstance(self.prompt_tokens, int)
            or isinstance(self.completion_tokens, bool)
            or not isinstance(self.completion_tokens, int)
            or isinstance(self.total_tokens, bool)
            or not isinstance(self.total_tokens, int)
        ):
            raise TypeError("Token counts must be non-boolean integers")
        if self.prompt_tokens < 0 or self.completion_tokens < 0 or self.total_tokens < 0:
            raise ValueError("Token counts must be non-negative")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError(
                f"total_tokens ({self.total_tokens}) must equal "
                f"prompt_tokens ({self.prompt_tokens}) + "
                f"completion_tokens ({self.completion_tokens})"
            )

    def __add__(self, other: Any) -> TokenUsage:
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    def is_monotonically_ge(self, other: TokenUsage) -> bool:
        """Check if self is greater than or equal to other across all token counts."""
        return (
            self.prompt_tokens >= other.prompt_tokens
            and self.completion_tokens >= other.completion_tokens
            and self.total_tokens >= other.total_tokens
        )

    @classmethod
    def zero(cls) -> TokenUsage:
        """Return a TokenUsage record with all counts initialized to zero."""
        return cls(prompt_tokens=0, completion_tokens=0, total_tokens=0)

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


class StorageVersionConflictError(StorageError):
    """Raised when an optimistic concurrency check fails on record update."""

    def __init__(
        self,
        message: str,
        *,
        record_id: str | None = None,
        expected_version: int | None = None,
        actual_version: int | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if record_id is not None:
            d["record_id"] = record_id
        if expected_version is not None:
            d["expected_version"] = expected_version
        if actual_version is not None:
            d["actual_version"] = actual_version
        super().__init__(
            message,
            operation="optimistic_update",
            details=d,
            recoverable=recoverable,
        )
        self.error_code = "STORAGE_VERSION_CONFLICT"
        self.record_id = record_id
        self.expected_version = expected_version
        self.actual_version = actual_version


class CrossNamespaceAccessError(StorageError):
    """Raised when an operation attempts to access or mutate records outside its namespace."""

    def __init__(
        self,
        message: str,
        *,
        requested_namespace: str | None = None,
        target_namespace: str | None = None,
        resource_id: str | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if requested_namespace is not None:
            d["requested_namespace"] = requested_namespace
        if target_namespace is not None:
            d["target_namespace"] = target_namespace
        if resource_id is not None:
            d["resource_id"] = resource_id
        super().__init__(
            message,
            operation="namespace_isolation_check",
            details=d,
            recoverable=recoverable,
        )
        self.error_code = "CROSS_NAMESPACE_ACCESS"
        self.requested_namespace = requested_namespace
        self.target_namespace = target_namespace
        self.resource_id = resource_id


class RecordNotFoundError(StorageError):
    """Raised when a requested persistent record does not exist in the specified namespace."""

    def __init__(
        self,
        message: str,
        *,
        record_id: str | None = None,
        namespace_id: str | None = None,
        table: str | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if record_id is not None:
            d["record_id"] = record_id
        if namespace_id is not None:
            d["namespace_id"] = namespace_id
        if table is not None:
            d["table"] = table
        super().__init__(
            message,
            operation="get_record",
            details=d,
            recoverable=recoverable,
        )
        self.error_code = "RECORD_NOT_FOUND"
        self.record_id = record_id
        self.namespace_id = namespace_id
        self.table = table


class TombstoneActiveError(StorageError):
    """Raised when an operation attempts to access or restore a tombstoned memory."""

    def __init__(
        self,
        message: str,
        *,
        target_memory_id: str | None = None,
        namespace_id: str | None = None,
        tombstone_id: str | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if target_memory_id is not None:
            d["target_memory_id"] = target_memory_id
        if namespace_id is not None:
            d["namespace_id"] = namespace_id
        if tombstone_id is not None:
            d["tombstone_id"] = tombstone_id
        super().__init__(
            message,
            operation="tombstone_guard",
            details=d,
            recoverable=recoverable,
        )
        self.error_code = "TOMBSTONE_ACTIVE"
        self.target_memory_id = target_memory_id
        self.namespace_id = namespace_id
        self.tombstone_id = tombstone_id


class StateResurrectionConflictError(StorageError):
    """Raised when checkpoint restoration encounters irreconcilable tombstone conflicts."""

    def __init__(
        self,
        message: str,
        *,
        conflicting_memory_ids: Sequence[MemoryId] = (),
        namespace_id: str | None = None,
        checkpoint_id: str | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if conflicting_memory_ids:
            d["conflicting_memory_ids"] = [str(m) for m in conflicting_memory_ids]
        if namespace_id is not None:
            d["namespace_id"] = namespace_id
        if checkpoint_id is not None:
            d["checkpoint_id"] = checkpoint_id
        if "recovery_hint" not in d:
            d["recovery_hint"] = (
                "Operator intervention required: tombstoned memory conflict prevents safe "
                "checkpoint restoration"
            )
        super().__init__(
            message,
            operation="checkpoint_restore",
            details=d,
            recoverable=recoverable,
        )
        self.error_code = "STATE_RESURRECTION_CONFLICT"
        self.conflicting_memory_ids = tuple(conflicting_memory_ids)
        self.namespace_id = namespace_id
        self.checkpoint_id = checkpoint_id


class StepCommitError(StorageError):
    """Raised when the atomic 4-phase step commit protocol fails and rolls back."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        step_id: str,
        run_id: str,
        namespace_id: str,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        d["phase"] = phase
        d["step_id"] = step_id
        d["run_id"] = run_id
        d["namespace_id"] = namespace_id
        super().__init__(
            message,
            operation="atomic_step_commit",
            details=d,
            recoverable=recoverable,
        )
        self.error_code = "STEP_COMMIT_FAILED"
        self.phase = phase
        self.step_id = step_id
        self.run_id = run_id
        self.namespace_id = namespace_id


class PrePersistenceSecurityError(StorageError):
    """Raised when data violates pre-persistence security, provenance, or redaction rules."""

    def __init__(
        self,
        message: str,
        *,
        rule_name: str,
        namespace_id: str,
        record_id: str | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        d["rule_name"] = rule_name
        d["namespace_id"] = namespace_id
        if record_id:
            d["record_id"] = record_id
        super().__init__(
            message,
            operation="pre_persistence_security",
            details=d,
            recoverable=False,
        )
        self.error_code = "PRE_PERSISTENCE_SECURITY_VIOLATION"
        self.rule_name = rule_name
        self.namespace_id = namespace_id
        self.record_id = record_id


class SerializationError(StorageError):
    """Raised when an object or argument payload cannot be canonically serialized for storage."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "serialization",
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        super().__init__(
            message,
            operation=operation,
            details=details,
            recoverable=recoverable,
        )
        self.error_code = "SERIALIZATION_ERROR"


class DirtyRollbackError(HarnessError):
    """Raised when an orphaned run with in-flight mutating tool side-effects is detected."""

    def __init__(
        self,
        message: str = "Active run was orphaned with unconfirmed mutating tool side-effects",
        *,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if "error_code" not in d:
            d["error_code"] = "PARTIAL_DIRTY_ROLLBACK"
        if "recovery_hint" not in d:
            d["recovery_hint"] = (
                "Inspect unconfirmed side effects and register compensation handlers "
                "or manually reconcile before resuming"
            )
        super().__init__(
            message,
            error_code="PARTIAL_DIRTY_ROLLBACK",
            details=d,
            recoverable=recoverable,
        )


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


class DiagnosticListenerError(HarnessError, RuntimeError):
    """Raised when a diagnostic event listener fails under strict mode."""

    def __init__(
        self,
        message: str | None = None,
        *,
        listener_name: str | None = None,
        event_type: str | None = None,
        original_exc: Exception | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if listener_name is not None:
            d["listener_name"] = listener_name
        if event_type is not None:
            d["event_type"] = event_type
        if original_exc is not None:
            d["original_error"] = str(original_exc)

        eff_msg = message or (
            f"Diagnostic listener {listener_name!r} failed on event {event_type!r}: {original_exc}"
            if listener_name and event_type
            else "Diagnostic listener execution failed"
        )
        super().__init__(
            eff_msg,
            error_code="DIAGNOSTIC_LISTENER_FAILURE",
            details=d,
            recoverable=recoverable,
        )
        self.listener_name = listener_name
        self.event_type = event_type
        self.original_exc = original_exc


# --- Schema Versioning and State Hash Contracts ---
SCHEMA_VERSION: int = 1


def compute_state_hash(payload: Mapping[str, Any] | SerializableRecord) -> str:
    """Compute deterministic SHA-256 state hash across canonical JSON serialization."""
    if isinstance(payload, SerializableRecord):
        canonical_str = payload.to_json()
    elif isinstance(payload, Mapping):
        serialized = _serialize_value(payload)
        canonical_str = json.dumps(
            serialized,
            indent=None,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    else:
        raise TypeError(
            f"payload must be Mapping or SerializableRecord, got {type(payload).__name__}"
        )
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


def verify_state_hash(
    payload: Mapping[str, Any] | SerializableRecord,
    expected_hash: str,
    *,
    raise_on_divergence: bool = True,
) -> bool:
    """Verify payload state hash against expected hash, optionally raising ReplayDivergenceError."""
    actual_hash = compute_state_hash(payload)
    if actual_hash == expected_hash:
        return True
    if raise_on_divergence:
        raise ReplayDivergenceError(
            f"State hash divergence: expected {expected_hash}, got {actual_hash}",
            expected_hash=expected_hash,
            actual_hash=actual_hash,
            details={
                "error_code": "REPLAY_DIVERGENCE",
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
            },
        )
    return False


class CancellationToken:
    """Thread-safe cooperative cancellation token supporting live signaling and callbacks.

    Cooperative cancellation requires model adapters and tools to periodically poll
    check_cancellation() or register an abort callback. Blocking adapters that execute
    synchronous C-level blocking operations without socket timeouts cannot be cooperatively
    interrupted until control returns to Python.
    """

    def __init__(self, cancelled: bool = False, reason: str | None = None) -> None:
        self._event = threading.Event()
        self._reason: str | None = reason
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        if cancelled:
            self._event.set()
            if not self._reason:
                self._reason = "Explicitly initialized as cancelled"

    @property
    def is_cancelled(self) -> bool:
        """Return True if cancellation has been requested."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """Return the cancellation reason if cancelled."""
        return self._reason

    def cancel(self, reason: str = "Execution was cancelled") -> None:
        """Signal cancellation thread-safely and invoke registered callbacks."""
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            self._event.set()
            callbacks = list(self._callbacks)
        for cb in callbacks:
            with contextlib.suppress(Exception):
                cb()

    def check_cancellation(self) -> None:
        """Raise ExecutionCancelledError if cancellation has been requested."""
        if self.is_cancelled:
            reason = self._reason or "Execution was cancelled"
            raise ExecutionCancelledError(
                f"Execution was cancelled: {reason}",
                details={
                    "error_code": "EXECUTION_CANCELLED",
                    "cancellation_reason": reason,
                    "recovery_hint": "Abort or resume after unpausing",
                },
            )

    def add_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback to be executed immediately or upon cancellation."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
        with contextlib.suppress(Exception):
            callback()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for cancellation signal up to timeout seconds. Return True if cancelled."""
        return self._event.wait(timeout)

    def __repr__(self) -> str:
        return f"CancellationToken(cancelled={self.is_cancelled}, reason={self._reason!r})"


@dataclass(frozen=True)
class ModelCallContext(SerializableRecord):
    """Runtime context provided to a ModelPort invocation."""

    run_id: RunId
    step_id: StepId
    namespace_id: NamespaceId
    budget_remaining_tokens: int
    deadline: datetime | None = None
    is_cancelled: bool = False
    cancellation_token: CancellationToken | None = None

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
        is_canc = self.is_cancelled or (
            self.cancellation_token is not None and self.cancellation_token.is_cancelled
        )
        if is_canc:
            reason = (
                self.cancellation_token.reason
                if self.cancellation_token is not None and self.cancellation_token.reason
                else "Execution was cancelled"
            )
            raise ExecutionCancelledError(
                f"Execution run '{self.run_id}' step '{self.step_id}' was cancelled: {reason}",
                details={
                    "run_id": str(self.run_id),
                    "step_id": str(self.step_id),
                    "error_code": "EXECUTION_CANCELLED",
                    "cancellation_reason": reason,
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
        ct_raw = data.get("cancellation_token")
        cancellation_token: CancellationToken | None = None
        if isinstance(ct_raw, CancellationToken):
            cancellation_token = ct_raw
        elif isinstance(ct_raw, Mapping):
            cancellation_token = CancellationToken(
                cancelled=bool(ct_raw.get("is_cancelled", False)),
                reason=ct_raw.get("reason"),
            )
        return cls(
            run_id=RunId(str(data["run_id"])),
            step_id=StepId(str(data["step_id"])),
            namespace_id=NamespaceId(str(data["namespace_id"])),
            budget_remaining_tokens=int(data["budget_remaining_tokens"]),
            deadline=_parse_dt(deadline_raw) if deadline_raw is not None else None,
            is_cancelled=bool(data.get("is_cancelled", False)),
            cancellation_token=cancellation_token,
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
    cancellation_token: CancellationToken | None = None
    business_operation_id: str | None = None

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
        is_canc = self.is_cancelled or (
            self.cancellation_token is not None and self.cancellation_token.is_cancelled
        )
        if is_canc:
            reason = (
                self.cancellation_token.reason
                if self.cancellation_token is not None and self.cancellation_token.reason
                else "Execution was cancelled"
            )
            raise ExecutionCancelledError(
                f"Tool invocation '{self.invocation_id}' was cancelled: {reason}",
                details={
                    "run_id": str(self.run_id),
                    "step_id": str(self.step_id),
                    "invocation_id": str(self.invocation_id),
                    "error_code": "EXECUTION_CANCELLED",
                    "cancellation_reason": reason,
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
        ct_raw = data.get("cancellation_token")
        cancellation_token: CancellationToken | None = None
        if isinstance(ct_raw, CancellationToken):
            cancellation_token = ct_raw
        elif isinstance(ct_raw, Mapping):
            cancellation_token = CancellationToken(
                cancelled=bool(ct_raw.get("is_cancelled", False)),
                reason=ct_raw.get("reason"),
            )
        return cls(
            run_id=RunId(str(data["run_id"])),
            step_id=StepId(str(data["step_id"])),
            namespace_id=NamespaceId(str(data["namespace_id"])),
            invocation_id=ToolInvocationId(str(data["invocation_id"])),
            idempotency_key=str(data["idempotency_key"]),
            timeout_seconds=float(data.get("timeout_seconds", 30.0)),
            deadline=_parse_dt(deadline_raw) if deadline_raw is not None else None,
            is_cancelled=bool(data.get("is_cancelled", False)),
            cancellation_token=cancellation_token,
            business_operation_id=data.get("business_operation_id"),
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
        object.__setattr__(
            self,
            "finish_reason",
            _coerce_enum(ModelFinishReason, self.finish_reason, "ModelDecision.finish_reason"),
        )
        if self.raw_response is not None:
            object.__setattr__(self, "raw_response", _freeze(self.raw_response))

        if not self.decision_id.strip():
            raise ValueError("ModelDecision.decision_id must not be empty")
        if not str(self.step_id).strip():
            raise ValueError("ModelDecision.step_id must not be empty")
        if isinstance(self.tokens_used, bool) or not isinstance(self.tokens_used, int):
            raise TypeError(
                f"ModelDecision.tokens_used must be int, got {type(self.tokens_used).__name__}"
            )
        if self.token_usage is not None and self.tokens_used == 0:
            object.__setattr__(self, "tokens_used", self.token_usage.total_tokens)
        elif self.token_usage is not None and self.tokens_used != self.token_usage.total_tokens:
            raise ValueError(
                f"ModelDecision.tokens_used ({self.tokens_used}) does not match "
                f"token_usage.total_tokens ({self.token_usage.total_tokens})"
            )
        if self.tokens_used < 0:
            raise ValueError(f"ModelDecision.tokens_used must be >= 0, got {self.tokens_used}")
        if self.finish_reason == ModelFinishReason.ERROR and self.failure_details is None:
            if self.content is not None and self.content.strip():
                object.__setattr__(
                    self,
                    "failure_details",
                    FailureDetails(
                        error_type="ModelExecutionError",
                        message=self.content,
                        step_id=self.step_id,
                        error_code="MODEL_ERROR",
                    ),
                )
            else:
                raise ValueError(
                    "ModelDecision with finish_reason ERROR must provide "
                    "failure_details or error content"
                )

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
        object.__setattr__(
            self,
            "status",
            _coerce_enum(ToolCallStatus, self.status, "ToolResult.status"),
        )
        object.__setattr__(
            self,
            "side_effect",
            _coerce_enum(SideEffectType, self.side_effect, "ToolResult.side_effect"),
        )

        if not str(self.invocation_id).strip():
            raise ValueError("ToolResult.invocation_id must not be empty")
        if not self.tool_name.strip():
            raise ValueError("ToolResult.tool_name must not be empty")
        if not self.idempotency_key.strip():
            raise ValueError("ToolResult.idempotency_key must not be empty")
        if isinstance(self.execution_time_ms, bool) or not isinstance(
            self.execution_time_ms, int | float
        ):
            raise TypeError(
                f"ToolResult.execution_time_ms must be float, "
                f"got {type(self.execution_time_ms).__name__}"
            )
        if math.isnan(self.execution_time_ms) or math.isinf(self.execution_time_ms):
            raise ValueError(
                f"ToolResult.execution_time_ms must be finite, got {self.execution_time_ms}"
            )
        if self.execution_time_ms < 0.0:
            raise ValueError(
                f"ToolResult.execution_time_ms must be >= 0.0, got {self.execution_time_ms}"
            )
        if self.status in (ToolCallStatus.FAILED, ToolCallStatus.OUTCOME_UNKNOWN):
            if self.error is None and self.failure_details is None:
                raise ValueError(
                    f"ToolResult with status {self.status.value} must provide "
                    "error or failure_details"
                )
            if self.failure_details is not None and self.error is None:
                object.__setattr__(self, "error", self.failure_details.message)
            elif self.error is not None and self.failure_details is None:
                object.__setattr__(
                    self,
                    "failure_details",
                    FailureDetails(
                        error_type="ToolExecutionError",
                        message=self.error,
                        error_code="TOOL_ERROR",
                    ),
                )
        elif self.status == ToolCallStatus.COMPLETED:
            if self.error is not None:
                raise ValueError("ToolResult with status COMPLETED must not have error")
            if self.failure_details is not None:
                raise ValueError("ToolResult with status COMPLETED must not have failure_details")

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
    origin: MemoryOrigin = MemoryOrigin.EXTERNAL_RETRIEVAL
    run_id: RunId | None = None
    branch_id: BranchId | None = None

    def __post_init__(self) -> None:
        refs = self.source_references if self.source_references is not None else ()
        object.__setattr__(self, "source_references", tuple(refs))
        meta = self.metadata if self.metadata is not None else {}
        object.__setattr__(self, "metadata", _freeze(meta))
        object.__setattr__(
            self,
            "kind",
            _coerce_enum(MemoryKind, self.kind, "MemoryRecord.kind"),
        )
        object.__setattr__(
            self,
            "state",
            _coerce_enum(MemoryState, self.state, "MemoryRecord.state"),
        )
        object.__setattr__(
            self,
            "origin",
            _coerce_enum(MemoryOrigin, self.origin, "MemoryRecord.origin"),
        )
        if self.run_id is not None:
            if not str(self.run_id).strip():
                raise ValueError("MemoryRecord.run_id must not be empty when provided")
            object.__setattr__(self, "run_id", RunId(str(self.run_id)))
        if self.branch_id is not None:
            if not str(self.branch_id).strip():
                raise ValueError("MemoryRecord.branch_id must not be empty when provided")
            object.__setattr__(self, "branch_id", BranchId(str(self.branch_id)))

        if not str(self.id).strip():
            raise ValueError("MemoryRecord.id must not be empty")
        if not str(self.namespace_id).strip():
            raise ValueError("MemoryRecord.namespace_id must not be empty")
        if not self.content.strip():
            raise ValueError("MemoryRecord.content must not be empty")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, int | float):
            raise TypeError(
                f"MemoryRecord.confidence must be float, got {type(self.confidence).__name__}"
            )
        if math.isnan(self.confidence) or math.isinf(self.confidence):
            raise ValueError(f"MemoryRecord.confidence must be finite, got {self.confidence}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"MemoryRecord.confidence must be between 0.0 and 1.0, got {self.confidence}"
            )
        if isinstance(self.salience, bool) or not isinstance(self.salience, int | float):
            raise TypeError(
                f"MemoryRecord.salience must be float, got {type(self.salience).__name__}"
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
        if isinstance(self.access_count, bool) or not isinstance(self.access_count, int):
            raise TypeError(
                f"MemoryRecord.access_count must be int, got {type(self.access_count).__name__}"
            )
        if self.access_count < 0:
            raise ValueError(f"MemoryRecord.access_count must be >= 0, got {self.access_count}")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError(f"MemoryRecord.version must be int, got {type(self.version).__name__}")
        if self.version < 1:
            raise ValueError(f"MemoryRecord.version must be >= 1, got {self.version}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        expires_at_raw = data.get("expires_at")
        run_id_raw = data.get("run_id")
        branch_id_raw = data.get("branch_id")
        origin_raw = data.get("origin")
        origin_val = (
            _coerce_enum(MemoryOrigin, origin_raw, "MemoryRecord.origin")
            if origin_raw is not None
            else MemoryOrigin.EXTERNAL_RETRIEVAL
        )
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
            origin=origin_val,
            run_id=RunId(str(run_id_raw)) if run_id_raw is not None else None,
            branch_id=BranchId(str(branch_id_raw)) if branch_id_raw is not None else None,
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
    origin: MemoryOrigin = MemoryOrigin.AGENT_OBSERVATION
    run_id: RunId | None = None
    branch_id: BranchId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "kind",
            _coerce_enum(MemoryKind, self.kind, "MemoryCandidate.kind"),
        )
        object.__setattr__(
            self,
            "validation_status",
            _coerce_enum(
                CandidateStatus,
                self.validation_status,
                "MemoryCandidate.validation_status",
            ),
        )
        object.__setattr__(
            self,
            "origin",
            _coerce_enum(MemoryOrigin, self.origin, "MemoryCandidate.origin"),
        )
        if self.run_id is not None:
            if not str(self.run_id).strip():
                raise ValueError("MemoryCandidate.run_id must not be empty when provided")
            object.__setattr__(self, "run_id", RunId(str(self.run_id)))
        if self.branch_id is not None:
            if not str(self.branch_id).strip():
                raise ValueError("MemoryCandidate.branch_id must not be empty when provided")
            object.__setattr__(self, "branch_id", BranchId(str(self.branch_id)))

        if not self.candidate_id.strip():
            raise ValueError("MemoryCandidate.candidate_id must not be empty")
        if not str(self.namespace_id).strip():
            raise ValueError("MemoryCandidate.namespace_id must not be empty")
        if not self.content.strip():
            raise ValueError("MemoryCandidate.content must not be empty")
        if not str(self.source_step_id).strip():
            raise ValueError("MemoryCandidate.source_step_id must not be empty")
        if isinstance(self.proposed_salience, bool) or not isinstance(
            self.proposed_salience, int | float
        ):
            raise TypeError(
                f"MemoryCandidate.proposed_salience must be float, "
                f"got {type(self.proposed_salience).__name__}"
            )
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
        origin_raw = data.get("origin")
        origin_val = (
            _coerce_enum(MemoryOrigin, origin_raw, "MemoryCandidate.origin")
            if origin_raw is not None
            else MemoryOrigin.AGENT_OBSERVATION
        )
        run_id_raw = data.get("run_id")
        branch_id_raw = data.get("branch_id")
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
            origin=origin_val,
            run_id=RunId(str(run_id_raw)) if run_id_raw is not None else None,
            branch_id=BranchId(str(branch_id_raw)) if branch_id_raw is not None else None,
        )


@dataclass(frozen=True)
class ScoredMemoryItem(SerializableRecord):
    """Scored memory search result with transparent component breakdown."""

    record: MemoryRecord
    score: float
    score_breakdown: Mapping[str, float] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        rec = self.record
        if not isinstance(rec, MemoryRecord):
            raise TypeError(
                f"ScoredMemoryItem.record must be MemoryRecord, got {type(rec).__name__}"
            )
        bd = self.score_breakdown if self.score_breakdown is not None else {}
        for k, v in bd.items():
            if not isinstance(k, str):
                raise TypeError(f"score_breakdown keys must be str, got {type(k).__name__}")
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise TypeError(f"score_breakdown values must be float, got {type(v).__name__}")
            if math.isnan(v) or math.isinf(v):
                raise ValueError(f"score_breakdown values must be finite, got {v}")
        object.__setattr__(self, "score_breakdown", _freeze(bd))

        if isinstance(self.score, bool) or not isinstance(self.score, int | float):
            raise TypeError(
                f"ScoredMemoryItem.score must be float, got {type(self.score).__name__}"
            )
        if math.isnan(self.score) or math.isinf(self.score):
            raise ValueError(f"ScoredMemoryItem.score must be finite, got {self.score}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        rec_raw = data["record"]
        record = rec_raw if isinstance(rec_raw, MemoryRecord) else MemoryRecord.from_dict(rec_raw)
        bd = data.get("score_breakdown", {})
        return cls(
            record=record,
            score=float(data["score"]),
            score_breakdown={str(k): float(v) for k, v in bd.items()},
        )


@dataclass(frozen=True)
class MemoryFilter(SerializableRecord):
    """Structured query filter for searching and retrieving scoped memories."""

    namespace_id: NamespaceId
    kinds: tuple[MemoryKind, ...] = field(default_factory=tuple)
    run_id: RunId | None = None
    branch_id: BranchId | None = None
    origin: MemoryOrigin | None = None
    state: MemoryState | None = MemoryState.ACTIVE
    as_of: datetime | None = None
    min_confidence: float | None = None
    min_salience: float | None = None
    query_text: str | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if not str(self.namespace_id).strip():
            raise ValueError("MemoryFilter.namespace_id must not be empty")

        raw_kinds = self.kinds if self.kinds is not None else ()
        coerced_kinds = tuple(_coerce_enum(MemoryKind, k, "MemoryFilter.kinds") for k in raw_kinds)
        object.__setattr__(self, "kinds", coerced_kinds)

        if self.run_id is not None:
            if not str(self.run_id).strip():
                raise ValueError("MemoryFilter.run_id must not be empty when provided")
            object.__setattr__(self, "run_id", RunId(str(self.run_id)))

        if self.branch_id is not None:
            if not str(self.branch_id).strip():
                raise ValueError("MemoryFilter.branch_id must not be empty when provided")
            object.__setattr__(self, "branch_id", BranchId(str(self.branch_id)))

        if self.origin is not None:
            object.__setattr__(
                self,
                "origin",
                _coerce_enum(MemoryOrigin, self.origin, "MemoryFilter.origin"),
            )

        if self.state is not None:
            object.__setattr__(
                self,
                "state",
                _coerce_enum(MemoryState, self.state, "MemoryFilter.state"),
            )

        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("MemoryFilter.as_of must be timezone-aware when provided")

        if self.min_confidence is not None:
            if isinstance(self.min_confidence, bool) or not isinstance(
                self.min_confidence, int | float
            ):
                raise TypeError(
                    "MemoryFilter.min_confidence must be float, "
                    f"got {type(self.min_confidence).__name__}"
                )
            if math.isnan(self.min_confidence) or math.isinf(self.min_confidence):
                raise ValueError(
                    f"MemoryFilter.min_confidence must be finite, got {self.min_confidence}"
                )
            if not (0.0 <= self.min_confidence <= 1.0):
                raise ValueError(
                    "MemoryFilter.min_confidence must be between 0.0 and 1.0, "
                    f"got {self.min_confidence}"
                )

        if self.min_salience is not None:
            if isinstance(self.min_salience, bool) or not isinstance(
                self.min_salience, int | float
            ):
                raise TypeError(
                    "MemoryFilter.min_salience must be float, "
                    f"got {type(self.min_salience).__name__}"
                )
            if math.isnan(self.min_salience) or math.isinf(self.min_salience):
                raise ValueError(
                    f"MemoryFilter.min_salience must be finite, got {self.min_salience}"
                )
            if not (0.0 <= self.min_salience <= 1.0):
                raise ValueError(
                    "MemoryFilter.min_salience must be between 0.0 and 1.0, "
                    f"got {self.min_salience}"
                )

        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError(f"MemoryFilter.limit must be int, got {type(self.limit).__name__}")
        if self.limit <= 0:
            raise ValueError(f"MemoryFilter.limit must be > 0, got {self.limit}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        raw_kinds = data.get("kinds", ())
        coerced_kinds = tuple(_coerce_enum(MemoryKind, k, "MemoryFilter.kinds") for k in raw_kinds)
        as_of_raw = data.get("as_of")
        as_of_val = _parse_dt(as_of_raw) if as_of_raw is not None else None
        state_raw = data.get("state")
        state_val = (
            _coerce_enum(MemoryState, state_raw, "MemoryFilter.state")
            if state_raw is not None
            else None
        )
        origin_raw = data.get("origin")
        origin_val = (
            _coerce_enum(MemoryOrigin, origin_raw, "MemoryFilter.origin")
            if origin_raw is not None
            else None
        )
        run_id_raw = data.get("run_id")
        branch_id_raw = data.get("branch_id")
        min_conf = data.get("min_confidence")
        min_sal = data.get("min_salience")
        return cls(
            namespace_id=NamespaceId(str(data["namespace_id"])),
            kinds=coerced_kinds,
            run_id=RunId(str(run_id_raw)) if run_id_raw is not None else None,
            branch_id=BranchId(str(branch_id_raw)) if branch_id_raw is not None else None,
            origin=origin_val,
            state=state_val,
            as_of=as_of_val,
            min_confidence=float(min_conf) if min_conf is not None else None,
            min_salience=float(min_sal) if min_sal is not None else None,
            query_text=data.get("query_text"),
            limit=int(data.get("limit", 50)),
        )


@dataclass(frozen=True)
class TombstoneRecord(SerializableRecord):
    """Durable record of logical/physical memory deletion for verifiable forgetting audit."""

    tombstone_id: str
    target_memory_id: MemoryId
    namespace_id: NamespaceId
    reason: str
    tombstoned_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.tombstone_id, str):
            raise TypeError(
                f"TombstoneRecord.tombstone_id must be str, got {type(self.tombstone_id).__name__}"
            )
        if not self.tombstone_id.strip():
            raise ValueError("TombstoneRecord.tombstone_id must not be empty")

        if not isinstance(self.target_memory_id, str):
            raise TypeError(
                "TombstoneRecord.target_memory_id must be str or MemoryId, "
                f"got {type(self.target_memory_id).__name__}"
            )
        if not str(self.target_memory_id).strip():
            raise ValueError("TombstoneRecord.target_memory_id must not be empty")

        if not isinstance(self.namespace_id, str):
            raise TypeError(
                "TombstoneRecord.namespace_id must be str or NamespaceId, "
                f"got {type(self.namespace_id).__name__}"
            )
        if not str(self.namespace_id).strip():
            raise ValueError("TombstoneRecord.namespace_id must not be empty")

        if not isinstance(self.reason, str):
            raise TypeError(f"TombstoneRecord.reason must be str, got {type(self.reason).__name__}")
        if not self.reason.strip():
            raise ValueError("TombstoneRecord.reason must not be empty")

        if not isinstance(self.tombstoned_at, datetime):
            raise TypeError(
                "TombstoneRecord.tombstoned_at must be datetime, "
                f"got {type(self.tombstoned_at).__name__}"
            )
        if self.tombstoned_at.tzinfo is None:
            raise ValueError("TombstoneRecord.tombstoned_at must be timezone-aware")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        tomb_at_raw = data["tombstoned_at"]
        tomb_at = tomb_at_raw if isinstance(tomb_at_raw, datetime) else _parse_dt(str(tomb_at_raw))
        return cls(
            tombstone_id=str(data["tombstone_id"]),
            target_memory_id=MemoryId(str(data["target_memory_id"])),
            namespace_id=NamespaceId(str(data["namespace_id"])),
            reason=str(data["reason"]),
            tombstoned_at=tomb_at,
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
        object.__setattr__(
            self,
            "kind",
            _coerce_enum(MemoryKind, self.kind, "ContextItem.kind"),
        )
        if not str(self.memory_id).strip():
            raise ValueError("ContextItem.memory_id must not be empty")
        if not self.content.strip():
            raise ValueError("ContextItem.content must not be empty")
        if isinstance(self.rank_score, bool) or not isinstance(self.rank_score, int | float):
            raise TypeError(
                f"ContextItem.rank_score must be float, got {type(self.rank_score).__name__}"
            )
        if math.isnan(self.rank_score) or math.isinf(self.rank_score):
            raise ValueError(f"ContextItem.rank_score must be finite, got {self.rank_score}")
        if isinstance(self.token_cost, bool) or not isinstance(self.token_cost, int):
            raise TypeError(
                f"ContextItem.token_cost must be int, got {type(self.token_cost).__name__}"
            )
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
        if isinstance(self.total_tokens, bool) or not isinstance(self.total_tokens, int):
            raise TypeError(
                f"ContextManifest.total_tokens must be int, got {type(self.total_tokens).__name__}"
            )
        if self.total_tokens < 0:
            raise ValueError(f"ContextManifest.total_tokens must be >= 0, got {self.total_tokens}")
        if isinstance(self.budget_limit, bool) or not isinstance(self.budget_limit, int):
            raise TypeError(
                f"ContextManifest.budget_limit must be int, got {type(self.budget_limit).__name__}"
            )
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
        if isinstance(self.step_number, bool) or not isinstance(self.step_number, int):
            raise TypeError(
                f"StepResult.step_number must be int, got {type(self.step_number).__name__}"
            )
        if self.step_number < 0:
            raise ValueError(f"StepResult.step_number must be >= 0, got {self.step_number}")
        if (
            isinstance(self.memories_proposed, bool)
            or not isinstance(self.memories_proposed, int)
            or isinstance(self.memories_committed, bool)
            or not isinstance(self.memories_committed, int)
        ):
            raise TypeError("Memory counts must be non-boolean integers")
        if self.memories_proposed < 0 or self.memories_committed < 0:
            raise ValueError("Memory counts must be >= 0")
        if isinstance(self.tokens_consumed, bool) or not isinstance(self.tokens_consumed, int):
            raise TypeError(
                f"StepResult.tokens_consumed must be int, got {type(self.tokens_consumed).__name__}"
            )
        if self.token_usage is not None:
            if self.tokens_consumed == 0 and self.token_usage.total_tokens > 0:
                object.__setattr__(self, "tokens_consumed", self.token_usage.total_tokens)
            elif self.tokens_consumed != self.token_usage.total_tokens:
                raise ValueError(
                    f"StepResult.tokens_consumed ({self.tokens_consumed}) does not match "
                    f"token_usage.total_tokens ({self.token_usage.total_tokens})"
                )
        elif self.decision.token_usage is not None:
            if self.tokens_consumed == 0 and self.decision.token_usage.total_tokens > 0:
                object.__setattr__(self, "tokens_consumed", self.decision.token_usage.total_tokens)
            if self.token_usage is None:
                object.__setattr__(self, "token_usage", self.decision.token_usage)
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
    step_history: tuple[StepResult, ...] = field(default_factory=tuple)
    tool_results: tuple[ToolResult, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _coerce_enum(RunStatus, self.status, "RunResult.status"),
        )
        object.__setattr__(
            self,
            "step_history",
            tuple(self.step_history if self.step_history is not None else ()),
        )
        object.__setattr__(
            self,
            "tool_results",
            tuple(self.tool_results if self.tool_results is not None else ()),
        )
        if not str(self.run_id).strip():
            raise ValueError("RunResult.run_id must not be empty")
        if not str(self.namespace_id).strip():
            raise ValueError("RunResult.namespace_id must not be empty")
        if isinstance(self.step_count, bool) or not isinstance(self.step_count, int):
            raise TypeError(
                f"RunResult.step_count must be int, got {type(self.step_count).__name__}"
            )
        if self.step_count < 0:
            raise ValueError(f"RunResult.step_count must be >= 0, got {self.step_count}")
        if isinstance(self.total_tokens_consumed, bool) or not isinstance(
            self.total_tokens_consumed, int
        ):
            raise TypeError(
                f"RunResult.total_tokens_consumed must be int, "
                f"got {type(self.total_tokens_consumed).__name__}"
            )
        if self.token_usage is not None:
            if self.total_tokens_consumed == 0 and self.token_usage.total_tokens > 0:
                object.__setattr__(self, "total_tokens_consumed", self.token_usage.total_tokens)
            elif self.total_tokens_consumed != self.token_usage.total_tokens:
                raise ValueError(
                    f"RunResult.total_tokens_consumed ({self.total_tokens_consumed}) "
                    f"does not match token_usage.total_tokens ({self.token_usage.total_tokens})"
                )
        if self.total_tokens_consumed < 0:
            raise ValueError(
                f"RunResult.total_tokens_consumed must be >= 0, got {self.total_tokens_consumed}"
            )
        if self.status in (RunStatus.FAILED, RunStatus.ABORTED):
            if self.error is None and self.failure_details is None:
                raise ValueError(
                    f"RunResult with status {self.status.value} must provide "
                    "error or failure_details"
                )
            if self.failure_details is not None and self.error is None:
                object.__setattr__(self, "error", self.failure_details.message)
            elif self.error is not None and self.failure_details is None:
                object.__setattr__(
                    self,
                    "failure_details",
                    FailureDetails(
                        error_type="HarnessError",
                        message=self.error,
                        error_code=f"RUN_{self.status.name}",
                    ),
                )
        elif self.status == RunStatus.COMPLETED:
            if self.error is not None:
                raise ValueError("RunResult with status COMPLETED must not have error")
            if self.failure_details is not None:
                raise ValueError("RunResult with status COMPLETED must not have failure_details")

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
        raw_steps = data.get("step_history", ())
        step_history = tuple(
            s if isinstance(s, StepResult) else StepResult.from_dict(s) for s in raw_steps
        )
        raw_tools = data.get("tool_results", ())
        tool_results = tuple(
            t if isinstance(t, ToolResult) else ToolResult.from_dict(t) for t in raw_tools
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
            step_history=step_history,
            tool_results=tool_results,
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


@dataclass(frozen=True)
class OrphanedRunReport(SerializableRecord):
    """Diagnostic outcome of orphaned run detection and reconciliation."""

    run_id: RunId
    namespace_id: NamespaceId
    previous_status: RunStatus
    new_status: RunStatus
    is_dirty: bool
    mutating_tool_invocations_count: int
    reconciled_steps_count: int
    reason: str
    failure_details: FailureDetails


@dataclass(frozen=True)
class ToolReconciliationReport(SerializableRecord):
    """Outcome report from reconciling pending/unknown tool invocations after a crash."""

    namespace_id: NamespaceId
    reconciled_count: int
    cached_replayed_count: int
    marked_unknown_count: int
    dirty_non_idempotent_count: int
    run_id: RunId | None = None


@runtime_checkable
class StoragePort(Protocol):
    """Port for durable multi-tier storage operations supporting full lifecycle persistence."""

    # --- Memory Operations (Superset of MemoryStoreProtocol) ---
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

    def search_scored(self, query_filter: MemoryFilter) -> Sequence[ScoredMemoryItem]:
        """Execute scoped, filtered search returning scored records with component breakdown."""
        ...

    def list_active(self, namespace_id: NamespaceId, kind: MemoryKind) -> Sequence[MemoryRecord]:
        """List active memories for a given category."""
        ...

    def tombstone(self, memory_id: MemoryId, namespace_id: NamespaceId, reason: str) -> None:
        """Logically delete and tombstone a memory record with verifiable audit."""
        ...

    def list_tombstoned_memory_ids(self, namespace_id: NamespaceId) -> frozenset[MemoryId]:
        """Return all tombstoned memory IDs in a namespace for anti-resurrection reconciliation."""
        ...

    def update_optimistic(self, record: MemoryRecord, expected_version: int) -> None:
        """Update a memory record conditionally verifying the expected version."""
        ...

    # --- Run & Step Operations ---
    def save_run(self, run_result: RunResult) -> None:
        """Persist or update run execution state."""
        ...

    def get_run(self, run_id: RunId, namespace_id: NamespaceId) -> RunResult | None:
        """Retrieve run state by identifier within a namespace."""
        ...

    def list_orphaned_runs(self, namespace_id: NamespaceId) -> Sequence[RunResult]:
        """List execution runs left in non-terminal states ('running', 'initialized')."""
        ...

    def save_step(self, step_result: StepResult, namespace_id: NamespaceId) -> None:
        """Persist a single step execution outcome."""
        ...

    def record_step_start(
        self,
        step_id: StepId,
        run_id: RunId,
        namespace_id: NamespaceId,
        step_number: int,
        started_at: datetime,
    ) -> None:
        """Record initial step execution state before dispatching decisions or tools."""
        ...

    # --- Tool Invocations ---
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
    ) -> None:
        """Record intent before tool dispatch for crash safety."""
        ...

    def record_tool_result(
        self,
        result: ToolResult,
        namespace_id: NamespaceId,
        *,
        completed_at: datetime | None = None,
    ) -> None:
        """Update tool invocation outcome after dispatch."""
        ...

    def get_tool_invocation(
        self,
        invocation_id: ToolInvocationId,
        namespace_id: NamespaceId,
    ) -> ToolResult | None:
        """Retrieve tool invocation outcome."""
        ...

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
        """Check for reusable cached outcome or block illegal non-idempotent re-execution."""
        return None

    # --- Checkpoints ---
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
        ...

    def get_checkpoint(
        self,
        checkpoint_id: CheckpointId,
        namespace_id: NamespaceId,
    ) -> Mapping[str, Any] | None:
        """Retrieve checkpoint state payload."""
        ...

    # --- Trace Events ---
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
        ...

    def get_trace_events(
        self,
        run_id: RunId,
        namespace_id: NamespaceId,
    ) -> Sequence[Mapping[str, Any]]:
        """Retrieve trace events for a run ordered by sequence number."""
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


@runtime_checkable
class ProviderIdempotentTool(ToolProtocol, Protocol):
    """Protocol for tools declaring external provider-supported idempotency."""

    supports_provider_idempotency: bool


__all__ = [
    "LEGAL_RUN_TRANSITIONS",
    "LEGAL_STEP_TRANSITIONS",
    "SCHEMA_VERSION",
    "BranchId",
    "BudgetExceededError",
    "CancellationToken",
    "CandidateStatus",
    "CheckpointId",
    "ClockPort",
    "CompensationFailedError",
    "ContextItem",
    "ContextManifest",
    "CrossNamespaceAccessError",
    "DeadlineExceededError",
    "DiagnosticListenerError",
    "DirtyRollbackError",
    "EventId",
    "ExecutionCancelledError",
    "FailureDetails",
    "FrozenDict",
    "HarnessError",
    "IdGeneratorPort",
    "InvalidStateTransitionError",
    "MemoryCandidate",
    "MemoryFilter",
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
    "OrphanedRunReport",
    "PolicyViolationError",
    "PrePersistenceSecurityError",
    "ProviderIdempotentTool",
    "RecordNotFoundError",
    "ReplayDivergenceError",
    "RunBudgets",
    "RunId",
    "RunResult",
    "RunStatus",
    "ScoredMemoryItem",
    "SerializableRecord",
    "SerializationError",
    "SideEffectType",
    "StateResurrectionConflictError",
    "StepCommitError",
    "StepId",
    "StepResult",
    "StepState",
    "StorageError",
    "StoragePort",
    "StorageVersionConflictError",
    "TokenUsage",
    "TombstoneActiveError",
    "TombstoneRecord",
    "ToolCallContext",
    "ToolCallRequest",
    "ToolCallStatus",
    "ToolExecutionError",
    "ToolInvocationId",
    "ToolProtocol",
    "ToolReconciliationReport",
    "ToolResult",
    "compute_state_hash",
    "to_canonical_json",
    "verify_state_hash",
]

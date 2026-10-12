"""Pydantic v2 validation layer for external untrusted boundaries.

Provides schema validation and parsing for external payloads (JSON strings, dicts,
deserialized API inputs) into strongly-typed, immutable harness domain records
without duplicating domain models.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from pydantic import TypeAdapter, ValidationError

from memory_aware_agent_harness.contracts import (
    ContextItem,
    ContextManifest,
    FailureDetails,
    HarnessError,
    MemoryCandidate,
    MemoryRecord,
    ModelDecision,
    RunBudgets,
    RunResult,
    StepResult,
    TokenUsage,
    ToolCallRequest,
    ToolResult,
)

T = TypeVar("T")


def _sanitize_pydantic_error(err: Mapping[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for k, v in err.items():
        if k == "ctx" and isinstance(v, Mapping):
            cleaned[k] = {str(ctx_k): str(ctx_v) for ctx_k, ctx_v in v.items()}
        elif k == "loc" and isinstance(v, (tuple, list)):
            cleaned[k] = [str(item) for item in v]
        elif isinstance(v, (str, int, float, bool)) or v is None:
            cleaned[k] = v
        elif isinstance(v, (tuple, list)):
            cleaned[k] = [
                str(item) if not isinstance(item, (str, int, float, bool)) else item for item in v
            ]
        else:
            cleaned[k] = str(v)
    return cleaned


class BoundaryValidationError(HarnessError):
    """Raised when external input fails Pydantic boundary schema validation."""

    def __init__(
        self,
        message: str,
        *,
        validation_error: ValidationError,
        target_type: str,
    ) -> None:
        errors = [_sanitize_pydantic_error(e) for e in validation_error.errors(include_url=False)]
        details = {
            "error_code": "BOUNDARY_VALIDATION_ERROR",
            "target_type": target_type,
            "errors": errors,
            "recovery_hint": ("Inspect error fields and ensure external payload adheres to schema"),
        }
        super().__init__(
            message,
            error_code="BOUNDARY_VALIDATION_ERROR",
            details=details,
            recoverable=False,
        )
        self.validation_error = validation_error
        self.target_type = target_type


_ADAPTER_CACHE: dict[type[Any], TypeAdapter[Any]] = {}


def get_adapter(record_cls: type[T]) -> TypeAdapter[T]:
    """Retrieve or create a cached Pydantic TypeAdapter for the given domain record class."""
    adapter = _ADAPTER_CACHE.get(record_cls)
    if adapter is None:
        adapter = TypeAdapter(record_cls)
        _ADAPTER_CACHE[record_cls] = adapter
    return cast(TypeAdapter[T], adapter)


def validate_external_input(record_cls: type[T], data: Any) -> T:
    """Validate and deserialize external untrusted input into a strongly-typed domain record.

    Accepts dicts, mappings, JSON strings, or bytes. Raises BoundaryValidationError
    with structured diagnostic fields if validation fails.
    """
    adapter = get_adapter(record_cls)
    try:
        if isinstance(data, (str, bytes)):
            if isinstance(data, str) and not data.strip().startswith(("{", "[")):
                # Handle bare strings or plain text that aren't JSON objects
                try:
                    parsed = json.loads(data)
                    return adapter.validate_python(parsed)
                except Exception as err:
                    raise BoundaryValidationError(
                        f"External input for {record_cls.__name__} is not valid JSON",
                        validation_error=ValidationError.from_exception_data(
                            title=record_cls.__name__,
                            line_errors=[
                                {
                                    "type": "json_invalid",
                                    "loc": (),
                                    "input": data,
                                    "ctx": {"error": str(err)},
                                }
                            ],
                        ),
                        target_type=record_cls.__name__,
                    ) from err
            return adapter.validate_json(data)
        if isinstance(data, Mapping):
            return adapter.validate_python(dict(data))
        return adapter.validate_python(data)
    except ValidationError as exc:
        raise BoundaryValidationError(
            f"Failed to validate external input for {record_cls.__name__}: {exc}",
            validation_error=exc,
            target_type=record_cls.__name__,
        ) from exc


def validate_memory_record(data: Any) -> MemoryRecord:
    """Validate external input into an immutable MemoryRecord."""
    return validate_external_input(MemoryRecord, data)


def validate_memory_candidate(data: Any) -> MemoryCandidate:
    """Validate external input into an immutable MemoryCandidate."""
    return validate_external_input(MemoryCandidate, data)


def validate_run_budgets(data: Any) -> RunBudgets:
    """Validate external input into an immutable RunBudgets record."""
    return validate_external_input(RunBudgets, data)


def validate_token_usage(data: Any) -> TokenUsage:
    """Validate external input into an immutable TokenUsage record."""
    return validate_external_input(TokenUsage, data)


def validate_failure_details(data: Any) -> FailureDetails:
    """Validate external input into an immutable FailureDetails record."""
    return validate_external_input(FailureDetails, data)


def validate_tool_call_request(data: Any) -> ToolCallRequest:
    """Validate external input into an immutable ToolCallRequest record."""
    return validate_external_input(ToolCallRequest, data)


def validate_tool_result(data: Any) -> ToolResult:
    """Validate external input into an immutable ToolResult record."""
    return validate_external_input(ToolResult, data)


def validate_model_decision(data: Any) -> ModelDecision:
    """Validate external input into an immutable ModelDecision record."""
    return validate_external_input(ModelDecision, data)


def validate_step_result(data: Any) -> StepResult:
    """Validate external input into an immutable StepResult record."""
    return validate_external_input(StepResult, data)


def validate_run_result(data: Any) -> RunResult:
    """Validate external input into an immutable RunResult record."""
    return validate_external_input(RunResult, data)


def validate_context_item(data: Any) -> ContextItem:
    """Validate external input into an immutable ContextItem record."""
    return validate_external_input(ContextItem, data)


def validate_context_manifest(data: Any) -> ContextManifest:
    """Validate external input into an immutable ContextManifest record."""
    return validate_external_input(ContextManifest, data)


__all__ = [
    "BoundaryValidationError",
    "get_adapter",
    "validate_context_item",
    "validate_context_manifest",
    "validate_external_input",
    "validate_failure_details",
    "validate_memory_candidate",
    "validate_memory_record",
    "validate_model_decision",
    "validate_run_budgets",
    "validate_run_result",
    "validate_step_result",
    "validate_token_usage",
    "validate_tool_call_request",
    "validate_tool_result",
]

"""Terminal RunResult builder enforcing completed/aborted/failed invariants.

Coordinates terminal execution state, token consumption accounting, and structured
failure taxonomy into canonical RunResult records.
"""

from __future__ import annotations

from memory_aware_agent_harness.contracts import (
    FailureDetails,
    RunResult,
    RunStatus,
)
from memory_aware_agent_harness.runtime.state import LifecycleState


class RunResultBuilder:
    """Builds canonical RunResult instances from completed LifecycleState objects."""

    def __init__(self, state: LifecycleState) -> None:
        self.state = state

    def build(
        self,
        *,
        final_output: str | None = None,
        trace_id: str | None = None,
        error: str | None = None,
        failure_details: FailureDetails | None = None,
    ) -> RunResult:
        """Construct a validated terminal RunResult record.

        Raises:
            RuntimeError: If the run is not in a terminal lifecycle status.
        """
        if not self.state.is_terminal:
            raise RuntimeError(
                f"Cannot build RunResult: run '{self.state.run_id}' "
                f"is in non-terminal status '{self.state.status.value}'"
            )

        # Resolve trace ID
        resolved_trace_id = trace_id
        if resolved_trace_id is None:
            resolved_trace_id = self.state.id_generator.generate_id(f"trace_{self.state.run_id}")

        # Resolve token accounting
        tokens = self.state.cumulative_tokens
        total_tokens = tokens.total_tokens

        # Resolve status-specific invariants
        eff_error: str | None = error
        eff_failure: FailureDetails | None = failure_details

        if self.state.status == RunStatus.COMPLETED:
            # COMPLETED must never carry error or failure_details
            eff_error = None
            eff_failure = None
        elif self.state.status in (RunStatus.FAILED, RunStatus.ABORTED):
            # Must carry failure_details and error
            if eff_failure is None:
                eff_failure = self.state.failure_details
            if eff_failure is None and eff_error is not None:
                eff_failure = FailureDetails(
                    error_type="HarnessError",
                    message=eff_error,
                    error_code=f"RUN_{self.state.status.name}",
                )
            elif eff_failure is None and eff_error is None:
                eff_failure = FailureDetails(
                    error_type="HarnessError",
                    message=f"Run terminated in status {self.state.status.value}",
                    error_code=f"RUN_{self.state.status.name}",
                )
            if eff_error is None and eff_failure is not None:
                eff_error = eff_failure.message
        else:
            # CANCELLED or BUDGET_EXHAUSTED can optionally carry failure details
            if eff_failure is None:
                eff_failure = self.state.failure_details
            if eff_error is None and eff_failure is not None:
                eff_error = eff_failure.message

        return RunResult(
            run_id=self.state.run_id,
            namespace_id=self.state.namespace_id,
            status=self.state.status,
            step_count=self.state.step_count,
            total_tokens_consumed=total_tokens,
            final_output=final_output,
            trace_id=resolved_trace_id,
            error=eff_error,
            token_usage=tokens,
            failure_details=eff_failure,
        )

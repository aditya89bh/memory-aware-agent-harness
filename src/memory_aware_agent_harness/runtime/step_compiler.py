"""Step outcome compiler and StepResult constructor with monotonic token accounting.

Transforms step execution artifacts (decisions, tool outputs, memory stats) into
validated, immutable StepResult records while enforcing token accounting invariants.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from memory_aware_agent_harness.contracts import (
    FailureDetails,
    ModelDecision,
    StepId,
    StepResult,
    StepState,
    TokenUsage,
    ToolResult,
)
from memory_aware_agent_harness.runtime.state import LifecycleState


class StepOutcomeCompiler:
    """Compiles step outputs into canonical StepResult instances and updates LifecycleState."""

    def __init__(self, state: LifecycleState) -> None:
        self.state = state

    def compile_step(
        self,
        decision: ModelDecision,
        *,
        tool_results: Sequence[ToolResult] = (),
        memories_proposed: int = 0,
        memories_committed: int = 0,
        token_usage: TokenUsage | None = None,
        failure_details: FailureDetails | None = None,
        completed_at: datetime | None = None,
    ) -> StepResult:
        """Compile execution outputs into a validated StepResult record.

        Enforces:
        - Step ID and step count resolution from LifecycleState.
        - Monotonic token usage recording into LifecycleState.
        - Step FSM terminal transition (COMPLETED or FAILED/ABORTED).
        - Consistent tokens_consumed count matching token_usage.total_tokens.
        """
        step_id = self.state.current_step_id
        if step_id is None:
            step_id = StepId(
                self.state.id_generator.generate_id(
                    f"step_{self.state.run_id}_{self.state.step_count}"
                )
            )

        if memories_committed > memories_proposed and memories_proposed > 0:
            raise ValueError(
                f"memories_committed ({memories_committed}) cannot exceed "
                f"memories_proposed ({memories_proposed})"
            )

        # Resolve effective token usage
        eff_tokens = token_usage
        if eff_tokens is None and decision.token_usage is not None:
            eff_tokens = decision.token_usage
        if eff_tokens is None:
            eff_tokens = TokenUsage.zero()

        # Monotonically track in state
        self.state.record_tokens(eff_tokens)

        # Resolve timestamp
        resolved_time = completed_at if completed_at is not None else self.state.clock.now()

        # Coordinate Step FSM transition if non-terminal
        step_fsm = self.state.current_step_fsm
        if step_fsm is not None and not step_fsm.is_terminal:
            if failure_details is not None:
                step_fsm.transition_to(StepState.FAILED, reason=failure_details.message)
            else:
                step_fsm.transition_to(StepState.COMPLETED, reason="step compilation completed")

        return StepResult(
            step_id=step_id,
            run_id=self.state.run_id,
            step_number=self.state.step_count,
            decision=decision,
            tool_results=tuple(tool_results),
            memories_proposed=memories_proposed,
            memories_committed=memories_committed,
            tokens_consumed=eff_tokens.total_tokens,
            completed_at=resolved_time,
            token_usage=eff_tokens,
            failure_details=failure_details,
        )

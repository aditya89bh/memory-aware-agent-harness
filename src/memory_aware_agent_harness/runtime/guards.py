"""Runtime budget exhaustion guards and limit enforcement.

Enforces step count limits, token consumption ceilings, and wall-clock deadlines
using RunBudgets contracts and structured BudgetExceededError taxonomy.
"""

from __future__ import annotations

from memory_aware_agent_harness.contracts import (
    BudgetExceededError,
    RunBudgets,
    RunStatus,
)
from memory_aware_agent_harness.runtime.state import LifecycleState


class BudgetGuard:
    """Enforces execution limits against RunBudgets parameters."""

    def __init__(self, budgets: RunBudgets | None = None) -> None:
        self.budgets = budgets

    def _get_budgets(self, state: LifecycleState) -> RunBudgets:
        return self.budgets if self.budgets is not None else state.budgets

    def check_step_budget(self, state: LifecycleState) -> None:
        """Verify that step count has not reached or exceeded max_steps limit."""
        budgets = self._get_budgets(state)
        budgets.check_step_budget(state.step_count)

    def check_token_budget(self, state: LifecycleState) -> None:
        """Verify that cumulative token usage has not exceeded max_tokens limit."""
        budgets = self._get_budgets(state)
        budgets.check_token_budget(state.cumulative_tokens.total_tokens)

    def check_time_budget(self, state: LifecycleState) -> None:
        """Verify that elapsed run execution time has not exceeded max_time_seconds limit."""
        budgets = self._get_budgets(state)
        budgets.check_time_budget(state.elapsed_seconds)

    def check_all(self, state: LifecycleState) -> None:
        """Execute all budget checks in deterministic sequence (step, token, time).

        Raises:
            BudgetExceededError: On the first detected limit breach.
        """
        self.check_step_budget(state)
        self.check_token_budget(state)
        self.check_time_budget(state)

    def evaluate_budget(self, state: LifecycleState) -> BudgetExceededError | None:
        """Evaluate all limits, returning BudgetExceededError if breached, or None if healthy."""
        try:
            self.check_all(state)
            return None
        except BudgetExceededError as exc:
            return exc

    def enforce(self, state: LifecycleState) -> bool:
        """Check budgets and transition state to BUDGET_EXHAUSTED if breached.

        Returns:
            True if budgets are healthy and execution may proceed.
            False if a budget was breached and run state transitioned to BUDGET_EXHAUSTED.
        """
        if state.status == RunStatus.BUDGET_EXHAUSTED:
            return False
        breach = self.evaluate_budget(state)
        if breach is None:
            return True

        state.failure_details = breach.to_failure_details(step_id=state.current_step_id)
        state.exhaust_budget(reason=breach.message)
        return False

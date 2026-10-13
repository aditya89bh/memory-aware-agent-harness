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

    def check_step_budget(self, state: LifecycleState, is_terminal: bool = False) -> None:
        """Verify that step count has not exceeded max_steps limit.

        If is_terminal is True (e.g. at run completion when STOP was chosen),
        step budget is breached only if step_count > max_steps.
        Otherwise (pre-step or nonterminal continuation), step_count >= max_steps
        breaches the budget.
        """
        budgets = self._get_budgets(state)
        if is_terminal:
            if state.step_count > budgets.max_steps:
                raise BudgetExceededError(
                    f"Step budget exceeded: executed {state.step_count} steps, "
                    f"max is {budgets.max_steps}",
                    budget_type="steps",
                    limit=budgets.max_steps,
                    observed=state.step_count,
                    details={
                        "error_code": "BUDGET_EXCEEDED",
                        "budget_type": "steps",
                        "limit": budgets.max_steps,
                        "observed": state.step_count,
                        "recovery_hint": (
                            "Increase RunBudgets.max_steps or decompose task into smaller sub-tasks"
                        ),
                    },
                )
        else:
            budgets.check_step_budget(state.step_count)

    def check_token_budget(self, state: LifecycleState) -> None:
        """Verify that cumulative token usage has not exceeded max_tokens limit."""
        budgets = self._get_budgets(state)
        budgets.check_token_budget(state.cumulative_tokens.total_tokens)

    def check_time_budget(self, state: LifecycleState) -> None:
        """Verify that elapsed run execution time has not exceeded max_time_seconds limit."""
        budgets = self._get_budgets(state)
        budgets.check_time_budget(state.elapsed_seconds)

    def check_all(self, state: LifecycleState, is_terminal: bool = False) -> None:
        """Execute all budget checks in deterministic sequence (step, token, time).

        Raises:
            BudgetExceededError: On the first detected limit breach.
        """
        self.check_step_budget(state, is_terminal=is_terminal)
        self.check_token_budget(state)
        self.check_time_budget(state)

    def check_pre_step(self, state: LifecycleState) -> None:
        """Enforce pre-step constraints (step_count >= max_steps, tokens > max, time > max)."""
        self.check_all(state, is_terminal=False)

    def check_mid_step(self, state: LifecycleState) -> None:
        """Enforce mid-step constraints (token and time limits, step count already started)."""
        self.check_token_budget(state)
        self.check_time_budget(state)

    def check_post_step(self, state: LifecycleState, is_terminal: bool = False) -> None:
        """Enforce post-step constraints."""
        self.check_all(state, is_terminal=is_terminal)

    def evaluate_budget(
        self, state: LifecycleState, is_terminal: bool = False
    ) -> BudgetExceededError | None:
        """Evaluate all limits, returning BudgetExceededError if breached, or None if healthy."""
        try:
            self.check_all(state, is_terminal=is_terminal)
            return None
        except BudgetExceededError as exc:
            return exc

    def evaluate_mid_step_budget(self, state: LifecycleState) -> BudgetExceededError | None:
        """Evaluate mid-step limits (token and time only)."""
        try:
            self.check_mid_step(state)
            return None
        except BudgetExceededError as exc:
            return exc

    def enforce_pre_step(self, state: LifecycleState) -> bool:
        """Enforce pre-step limits before starting a step.

        Returns True if execution may proceed, False if budget exhausted.
        """
        if state.status == RunStatus.BUDGET_EXHAUSTED:
            return False
        breach = self.evaluate_budget(state, is_terminal=False)
        if breach is None:
            return True

        state.failure_details = breach.to_failure_details(step_id=state.current_step_id)
        state.exhaust_budget(reason=breach.message)
        return False

    def enforce_mid_step(self, state: LifecycleState) -> bool:
        """Enforce token and time limits during step execution.

        Returns True if execution may proceed, False if budget exhausted.
        """
        if state.status == RunStatus.BUDGET_EXHAUSTED:
            return False
        breach = self.evaluate_mid_step_budget(state)
        if breach is None:
            return True

        state.failure_details = breach.to_failure_details(step_id=state.current_step_id)
        state.exhaust_budget(reason=breach.message)
        return False

    def enforce_post_step(self, state: LifecycleState, is_terminal: bool = False) -> bool:
        """Enforce post-step limits after step execution.

        If is_terminal is True (model STOP), step limit is step_count > max_steps.
        If is_terminal is False, step limit is step_count >= max_steps.
        Returns True if healthy, False if budget exhausted.
        """
        if state.status == RunStatus.BUDGET_EXHAUSTED:
            return False
        breach = self.evaluate_budget(state, is_terminal=is_terminal)
        if breach is None:
            return True

        state.failure_details = breach.to_failure_details(step_id=state.current_step_id)
        state.exhaust_budget(reason=breach.message)
        return False

    def enforce(self, state: LifecycleState, is_terminal: bool = False) -> bool:
        """Check budgets and transition state to BUDGET_EXHAUSTED if breached."""
        return self.enforce_post_step(state, is_terminal=is_terminal)

# ADR-0006: Internal State Rollback vs. Fallible Side-Effect Compensation

## Status
Accepted

## Context
When rolling back an agent execution to a previous checkpoint, naive systems assume they can simply rewind the step counter and replay. However, in real-world systems, external tools execute mutating actions (e.g., sending HTTP requests, writing files, sending emails, calling external payment gateways). Internal state (memory, step counters) can be cleanly rolled back, but external physical mutations cannot be un-executed by rewinding software state.

## Decision
We enforce a strict boundary separating **Internal State Rollback** from **External Side Effects**:
1. Tool invocations are strictly categorized into `READ_ONLY`, `IDEMPOTENT_WRITE`, and `NON_IDEMPOTENT_WRITE`.
2. Rolling back to a checkpoint $K$ audits all tool invocations that occurred after $K$.
3. If only `READ_ONLY` or idempotent actions occurred, the rollback is clean (`CLEAN_ROLLBACK`).
4. If `NON_IDEMPOTENT_WRITE` actions occurred, the rollback is flagged as `PARTIAL_DIRTY_ROLLBACK`.
5. The harness executes registered compensation handlers in reverse order, but treats compensation as **optional, fallible, and incapable of guaranteeing reversal**.
6. Any failed compensation halts automated execution, logs `COMPENSATION_FAILED`, and requests operator mediation.

## Consequences
- **Positive:** Honest operational semantics; prevents dangerous blind repetition of external mutations; maintains audit trails of uncompensated side effects.
- **Negative:** Requires developers to write compensation handlers where possible and handle dirty rollback states.
- **Reversal Trigger:** None; fundamental physical boundary of software execution.

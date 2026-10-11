# ADR-0011: Strong Type Identifiers (NewType) and PEP 561 Typing

## Status
Accepted

## Context
In complex agent architectures, many domain identifiers are string-based (e.g., `run_id`, `step_id`, `memory_id`, `namespace_id`, `checkpoint_id`, `branch_id`). Using raw `str` type hints allows accidental transposition bugs (e.g., passing a `memory_id` into a function expecting a `namespace_id`), which static type checkers cannot detect. Furthermore, downstream libraries consuming the harness package cannot verify type hints without a standard PEP 561 `py.typed` marker.

## Decision
We enforce **Strong Type Identifiers and PEP 561 Typing**:
1. All domain identifiers in `contracts.py` are defined using `typing.NewType("IdentifierName", str)`:
   - `RunId`
   - `StepId`
   - `MemoryId`
   - `NamespaceId`
   - `CheckpointId`
   - `BranchId`
   - `EventId`
   - `ToolInvocationId`
2. The package includes an empty `py.typed` marker at `src/memory_aware_agent_harness/py.typed` conforming to PEP 561.
3. All internal code and test suites run under `mypy --strict` with zero type errors.

## Consequences
- **Positive:** Compile-time prevention of argument transposition; self-documenting APIs; full type safety for third-party consumers.
- **Negative:** Requires explicit wrapping with constructor functions (e.g., `MemoryId("mem_01")`) when instantiating records with raw strings in typed contexts.
- **Reversal Trigger:** None; fundamental typing best practice for robust Python libraries.

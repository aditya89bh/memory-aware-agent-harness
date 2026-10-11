# ADR-0009: Phase 0 Packaging, Tooling, and CI Scaffolding Strategy

## Status
Accepted

## Context
In multi-phase development plans, teams frequently defer packaging configuration (`pyproject.toml`), linters (Ruff), strict typing (Mypy), and CI workflows to the final phase. This creates high risk: late-stage packaging discoveries often expose fundamental dependency conflicts, circular imports, formatting drift, and type-system incompatibilities that require expensive refactoring.

## Decision
We introduce **Packaging, Static Analysis Tooling, and CI Scaffolding in Phase 0**:
1. Phase 0 configures a minimal, standards-compliant `pyproject.toml` (using `hatchling` or `flit_core`), Ruff configuration, strict Mypy settings, and basic GitHub Actions CI.
2. The initial package skeleton defines public exports and typing contracts without implementing runtime logic.
3. Every subsequent phase (Phases 1 through 12) immediately benefits from continuous linting, strict type verification, and automated CI validation on every task commit.
4. Final packaging in Phase 12 is restricted to version tagging, release artifact builds, and distribution publishing.

## Consequences
- **Positive:** Immediate enforcement of engineering standards; zero type-checking or packaging surprises in later phases; continuous validation from Day 1.
- **Negative:** Requires setup effort in Phase 0 before implementing runtime features.
- **Reversal Trigger:** None; standard modern software engineering practice.

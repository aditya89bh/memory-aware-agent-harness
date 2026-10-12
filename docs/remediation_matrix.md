# Independent Review Remediation Matrix

**Repository:** `aditya89bh/memory-aware-agent-harness`  
**Reference Commit Under Review:** `66481a9`  
**Review Status:** BLOCK (Remediated)  
**Date of Remediation:** 2026-10-11  

---

## 1. High-Severity Findings Matrix (H-01 through H-07)

| Finding ID | Severity | Finding Summary | Resolution & Architectural Remedy | Commit Hash | Key Verification Tests |
|---|:---:|---|---|:---:|---|
| **H-01** | **High** | Public records lack deep immutability; vulnerable to alias mutations from caller references; missing canonical serialization. | Implemented recursive `_freeze()` converting nested dicts to `MappingProxyType` and sequences to `tuple`. Created `SerializableRecord` base class with canonical `to_dict()`, `from_dict()`, `to_json()`, and `from_json()` enforcing sorted keys, compact separators, and ISO-8601 UTC datetimes. | `608046a` | `tests/unit/test_contracts_serialization.py`: `test_tool_call_request_alias_mutation_defense`, `test_memory_record_alias_mutation_defense`, `test_all_contract_records_json_roundtrip` |
| **H-02** | **High** | Incomplete Run and Step FSM lifecycle contracts; missing legal transition table, cancellation, and orphaned-run recovery. | Defined `StepState` enum, `LEGAL_RUN_TRANSITIONS` and `LEGAL_STEP_TRANSITIONS` immutable transition matrices. Documented orphaned run recovery semantics (`RUNNING` $\rightarrow$ `FAILED`/`PAUSED`) on crash recovery. | `5a85a4d`, `4577952` | `tests/unit/test_fsm_transitions.py`: `test_legal_run_transitions_matrix`, `test_legal_step_transitions_matrix`; `tests/failure/test_contracts_failures.py`: `test_illegal_run_transitions_rejected`, `test_illegal_step_transitions_rejected` |
| **H-03** | **High** | Missing structured execution call contexts, token accounting, deterministic clock/ID generator ports. | Defined `ModelCallContext`, `ToolCallContext`, `TokenUsage` (with arithmetic balance assertions), `RunBudgets`, `FailureDetails`, `ClockPort`, and `IdGeneratorPort`. Implemented `DeterministicClock` and `DeterministicIdGenerator` in `doubles.py`. | `5a85a4d`, `3d02e60` | `tests/unit/test_fsm_transitions.py`: `test_token_usage_validation_and_serialization`, `test_clock_and_id_generator_ports_runtime_check`; `tests/unit/test_doubles.py`: all tests |
| **H-04** | **High** | Missing crash-safe step commit protocol, tool invocation intent registration, and idempotency uniqueness constraint. | Added intent-driven 4-phase commit protocol: (1) `INTENT_RECORDED`, (2) `TOOL_DISPATCH`, (3) `OUTCOME_UPDATE`, (4) `ATOMIC_STEP_COMMIT`. Added `UNIQUE(namespace_id, tool_name, idempotency_key)` constraint on `tool_invocations` table. Defined `OUTCOME_UNKNOWN` handling during crash recovery. | `4577952` | `tests/failure/test_contracts_failures.py`: `test_orphaned_run_and_unknown_outcome_contract` |
| **H-05** | **High** | Erroneous rollback specification claiming idempotent writes are reversible. Missing branch visibility semantics. | Corrected rollback model in `SPEC.md`, `ARCHITECTURE.md`, and ADR-0006: only `READ_ONLY` actions produce `CLEAN_ROLLBACK`. Any `IDEMPOTENT_WRITE` or `NON_IDEMPOTENT_WRITE` mutates external reality and flags `PARTIAL_DIRTY_ROLLBACK`. Defined branch visibility isolation via `branch_id`. | `7e7457d`, `4577952` | `tests/failure/test_contracts_failures.py`: `test_orphaned_run_and_unknown_outcome_contract` |
| **H-06** | **High** | Unsupported physical erasure claims on wear-leveled SSDs/WAL. Missing derived memory erasure propagation. | Clarified software deletion vs physical destruction: SQLite `DELETE` + `PRAGMA wal_checkpoint(TRUNCATE)` purges database and WAL frames, but wear-leveling flash cells cannot be guaranteed destroyed by POSIX calls. Specified lineage propagation: derived memories referencing purged records are flagged `origin_status = 'ORIGIN_PURGED'`. | `7e7457d`, `4577952` | `SPEC.md` Section 6, `ARCHITECTURE.md` Section 8, ADR-0008 |
| **H-07** | **High** | Vulnerability to prompt injection delimiter breakout. Missing trust categorization, namespace isolation, realistic integrity. | Added mandatory XML escaping (`&lt;`, `&gt;`, `&amp;`, `&quot;`, `&apos;`) for untrusted memory content in prompt fences. Defined `MemoryOrigin` enum (`SYSTEM_PROVIDED`, `USER_EXPLICIT`, `AGENT_OBSERVATION`, `DERIVED_CONSOLIDATION`, `EXTERNAL_RETRIEVAL`). Enforced namespace isolation (`WHERE namespace_id = ?`). Grounded integrity in SHA-256 state hashing. | `7e7457d`, `4577952` | `tests/failure/test_contracts_failures.py`: `test_memory_origin_enumeration_coverage` |

---

## 2. Medium-Severity Findings Matrix (M-01 through M-11)

| Finding ID | Finding Summary | Resolution | Commit Hash | Key Verification Evidence |
|---|---|---|:---:|---|
| **M-01** | Raw `str` identifiers permit transposition bugs. | Converted all domain identifiers (`RunId`, `StepId`, `MemoryId`, `NamespaceId`, `CheckpointId`, `BranchId`, `EventId`, `ToolInvocationId`) to PEP 484 `NewType`. | `fa11fd1` | Mypy strict type checking passes across all modules and tests. |
| **M-02** | Missing PEP 561 `py.typed` marker for library consumers. | Created `src/memory_aware_agent_harness/py.typed` and registered it in `pyproject.toml` `package-data`. | `fa11fd1`, `cb4c4da` | Wheel smoke test verifies `(r.files(...) / 'py.typed').is_file()`. |
| **M-03** | Dataclasses lacked field-level contract validation. | Added `__post_init__` validation across all records enforcing bounds, non-empty IDs, and positive counters. | `fa11fd1`, `5a85a4d` | `tests/unit/test_contracts_validation.py` (17 tests). |
| **M-04** | Missing `CandidateStatus` enum; used raw string in dataclass. | Introduced `CandidateStatus` enum (`PENDING`, `APPROVED`, `REJECTED`) with `PENDING` default. | `fa11fd1` | `test_memory_candidate_validation`. |
| **M-05** | Naive datetimes accepted without timezone awareness. | Enforced timezone awareness (`tzinfo is not None`) in all datetime fields and parsed ISO strings. | `fa11fd1`, `608046a` | `test_memory_record_requires_tz_aware_datetimes`. |
| **M-06** | Duplication of 300+ lines of Python code in `ARCHITECTURE.md`. | Replaced duplicated code block in Section 4 with concise architectural mapping pointing to `contracts.py`. | `4577952` | Architecture document maintains single source of truth without code drift. |
| **M-07** | Missing root `LICENSE` file; packaging metadata gaps. | Created root `LICENSE` (Apache 2.0). Updated `pyproject.toml` with SPDX license, URLs, and package data. | `cb4c4da` | Built sdist and wheel include `LICENSE` and valid metadata. |
| **M-08** | Pre-commit Ruff version and config misaligned with `pyproject.toml`. | Updated `.pre-commit-config.yaml` to Ruff v0.9.9 passing `--config=pyproject.toml`. | `98b414f` | Local checks and pre-commit hooks fully aligned. |
| **M-09** | CI lacked fresh-venv wheel installation verification. | Updated `.github/workflows/ci.yml` `build-smoke` job to install wheel into an isolated venv and run external tests. | `98b414f` | Local verification succeeded; CI executes isolated test outside workspace. |
| **M-10** | Insufficient negative-path and failure contract tests. | Created `tests/failure/test_contracts_failures.py` covering malformed JSON, out-of-bounds, and illegal jumps. | `dcf1ebe` | 9 substantive failure tests passing. |
| **M-11** | Phase 0 commit accounting did not reflect remediation tasks. | Updated `ROADMAP.md` Section 1 and Section 2 to account for all 28 Phase 0 tasks and commits. | `1a27416` | Full accounting table with target allocation of 352 commits. |

---

## 3. Verification Suite Evidence

- **Linting (`ruff check src tests`):** All checks passed (0 errors).
- **Formatting (`ruff format --check src tests`):** 16 files already formatted.
- **Type Checking (`mypy --strict src tests`):** `Success: no issues found in 16 source files`.
- **Test Suite (`pytest -v`):** 42 passed in 0.04s (100% pass rate).
- **Package Build (`python -m build`):** Sdist (`.tar.gz`) and Wheel (`.whl`) built with zero warnings.
- **Wheel Smoke Test:** Tested in isolated virtual environment; verifies package version, public contracts, deterministic doubles, and `py.typed` marker.
- **Git State:** Clean working tree; strict author attribution to `Aditya bhatia <aditya89bh@gmail.com>`. Zero runtime code written (strict Phase 0 boundaries preserved).

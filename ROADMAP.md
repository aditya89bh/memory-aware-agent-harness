# ROADMAP.md: 13-Phase Master Development Roadmap

**Status:** REVISED (Phase 0 Independent Review Remediation)  
**Governing Rule:** No application or runtime implementation until `SPEC.md`, `ARCHITECTURE.md`, and `ROADMAP.md` are jointly approved and locked.  
**Planning Target:** 352 meaningful commits across 13 phases (acceptable range: 300–360 commits). *Target, not an artificial quota.*

---

## 1. Master Commit Allocation & Milestones

| Phase | Focus Area | Target Commits | Cumulative Commits | Milestone / Release Gate |
|---|---|:---:|:---:|---|
| **Phase 0** | Foundation, Specs, Package Setup & Remediation | 28 | 28 | Project skeleton, typed contracts, & approved specs |
| **Phase 1** | Core Runtime & Lifecycle | 30 | 58 | FSM execution & budget bounds |
| **Phase 2** | Memory Model & Storage Abstractions | 34 | 92 | Unified SQLite WAL persistence |
| **Phase 3** | Retrieval, Ranking, Context Compiler | 34 | 126 | Multi-signal ranker & fenced context |
| **Phase 4** | Memory Writes, Consolidation, Forgetting | 30 | 156 | Quarantined writes & verifiable deletion |
| **Phase 5** | Tool Runtime & Routing | 26 | 182 | **v0.1.0 (Core Lifecycle MVP)** |
| **Phase 6** | Checkpointing, Resume, Rollback, Branching | 26 | 208 | **v0.2.0 (Continuity & Recovery)** |
| **Phase 7** | Tracing, Provenance, Event Model | 28 | 236 | Lineage graph & PII redaction |
| **Phase 8** | Replay & Deterministic Hooks | 24 | 260 | **v0.3.0 (Governance & Replay)** |
| **Phase 9** | Evaluation Framework & Metrics | 26 | 286 | 3-baseline comparison runner |
| **Phase 10** | Benchmarks & Regression Suites | 22 | 308 | **v0.4.0 (Evaluation & Benchmarks)** |
| **Phase 11** | Integrations & Realistic Demos | 20 | 328 | 3 runnable end-to-end scenarios |
| **Phase 12** | Hardening, Docs, Packaging, Release | 24 | 352 | **v1.0.0 (Production Hardened)** |
| **Total** | | **352** | | |

---

## 2. Accounting of Phase 0 Commits

### 2.1 Baseline Phase 0 Commits (Commits 1 to 16)
The following 16 baseline commits established Phase 0 foundation:
1. `9535535`: `docs: add draft spec specification`
2. `45b66c8`: `docs: add draft architecture specification`
3. `96d59df`: `docs: add draft roadmap specification`
4. `69c2794`: `docs(spec): reconcile lifecycle, storage contracts, and retention`
5. `5dbafdf`: `docs(architecture): specify deep immutability, compensation, and DDL`
6. `f3ec6cf`: `docs(roadmap): expand to 13-phase development plan with release milestones`
7. `475b7f7`: `docs(adr): add architecture decision records ADR-001 through ADR-010`
8. `2b26090`: `docs(security): define threat model, injection defenses, and boundaries`
9. `b9bfdd2`: `chore(packaging): configure pyproject.toml with toolchain settings`
10. `1bf1660`: `chore(lint): configure ruff rules and pre-commit hooks`
11. `2a373fe`: `chore(types): configure strict mypy settings`
12. `ffab94e`: `chore(package): initialize package skeleton and version metadata`
13. `2f9f116`: `feat(contracts): define public type contracts, protocols, and exceptions`
14. `279aa28`: `test(skeleton): create pytest directory structure and fixtures`
15. `b9d21ee`: `ci(github): add workflow for linting, typing, and smoke testing`
16. `66481a9`: `docs(report): complete phase 0 report and smoke validation`

### 2.2 Independent Review Remediation Tasks (Commits 17 to 28)
Addressing Claude Code independent architecture review findings (H-01 through H-07, and Medium findings):
- **Commit 17 (Task 0.17):** Ingest independent review findings and formulate 12-task remediation plan.
- **Commit 18 (Task 0.18):** `fix(contracts): add NewType identifiers, CandidateStatus, and validation rules` (PEP 484 NewType IDs, bounds validation).
- **Commit 19 (Task 0.19):** `feat(contracts): implement deep immutability, normalization, and serialization` (Resolves H-01: recursive freezing, alias defense, canonical JSON roundtrips).
- **Commit 20 (Task 0.20):** `feat(contracts): define FSM states, transition matrix, and execution contexts` (Resolves H-02, H-03, H-04: StepState, transition tables, RunBudgets, TokenUsage, contexts, Clock/Id ports).
- **Commit 21 (Task 0.21):** `feat(replay): add deterministic test doubles for clock and id generator` (Resolves H-03: DeterministicClock and DeterministicIdGenerator).
- **Commit 22 (Task 0.22):** `docs(spec): reconcile rollback semantics, erasure propagation, and threat model` (Resolves H-05, H-06, H-07: mutating write rollback, erasure propagation, XML escaping, MemoryOrigin).
- **Commit 23 (Task 0.23):** `docs(architecture): update FSM, step commit protocol, and prune duplicate code` (Resolves H-02, H-04, H-05, ADR-0006/8/10/11 updates).
- **Commit 24 (Task 0.24):** `docs(roadmap): update Phase 0 remediation accounting and milestone gates` (This task: master plan accounting).
- **Commit 25 (Task 0.25):** `chore(license): add Apache-2.0 license and fix packaging metadata` (Root LICENSE, pyproject metadata).
- **Commit 26 (Task 0.26):** `chore(ci): align ruff configuration and add wheel installation smoke test` (Pre-commit alignment, wheel smoke test in CI).
- **Commit 27 (Task 0.27):** `test(remediation): add comprehensive negative-path and failure test suite` (Negative contract tests, crash boundaries, budget bounds).
- **Commit 28 (Task 0.28):** `docs(report): compile independent review remediation matrix and validation evidence` (Final matrix and Phase 0 completion report).

---

## 3. Mandatory Development & Governance Workflow

Every phase follows a strict 5-stage collaborative governance lifecycle:

```mermaid
flowchart LR
    S1[1. Scoping & Alignment: ChatGPT] --> S2[2. Implementation & Tests: Antigravity CLI]
    S2 --> S3[3. Independent Review: Claude Code]
    S3 --> S4[4. Remediation & Regression: Antigravity CLI]
    S4 --> S5[5. CI Verification & Gate Lock: GitHub CI]
```

1. **Scoping & Alignment (ChatGPT):** Confirm specific tasks, update acceptance criteria, and approve design changes for the phase.
2. **Implementation & Tests (Antigravity CLI):** Implement code **one coherent task per commit**. Write substantive unit, integration, and failure tests.
3. **Independent Review (Claude Code):** Perform comprehensive architectural, security, correctness, and maintainability audit. Output ranked findings (Critical, High, Medium, Low).
4. **Remediation & Regression (Antigravity CLI):** Fix all verified findings and implement explicit regression tests.
5. **CI Verification & Phase Lock (GitHub CI):** Run lint, type check, test suite, and build checks. Close and tag phase only when 100% green.

---

## 4. Git Commit & Push Rules

### 4.1 Commit Rules
- **One completed coherent task = one commit.**
- Never combine unrelated tasks; never artificially split tasks to pad commit counts.
- Commit message format: `<type>(<scope>): <concise description>` (e.g., `feat(runtime): implement FSM state transition table`).
- Commit locally after every completed task.
- Validate the task (lint, type check, test) before committing.
- Do not include agent co-author trailers or tool branding in commit metadata.

### 4.2 Push Rules
- **Push to GitHub only after the entire phase is completed and validated.**
- All quality checks (Ruff, strict Mypy, Pytest, build smoke tests, Claude Code review) must be 100% green before pushing.
- No unresolved Critical or High review findings permitted at push time.
- If GitHub CI fails after pushing, fix failures through additional task-scoped commits and report the phase as incomplete until green.

---

## 5. Testing Methodology & Quality Standards

By project completion (Phase 12), the test suite must satisfy:
- **Planning Expectation:** 200–300 substantive tests minimum.
- **Substantive Test Rule:** Tests must evaluate real functional logic, boundary limits, and failure modes. Zero dummy or empty test assertions.
- **Failure Path & Edge Case Coverage:**
  - Database locks, corrupted files, and transaction rollbacks.
  - Step, token, and wall-clock budget exhaustion.
  - Model timeout, malformed JSON, and unexpected finish reasons.
  - Tool execution exceptions, schema mismatches, and timeouts.
  - Prompt injection attacks embedded in retrieved memory records.
  - Cross-namespace isolation attacks (verifying 0% data leakage).
  - Verifiable forgetting compliance (verifying 0% retrieval of deleted/expired items).
  - Dirty rollback detection and fallible compensation failures.
- **Zero-API-Key Offline Testing:** Core test suites must run completely offline without external network calls or paid LLM API keys using deterministic test doubles.

---

## 6. Benchmark Methodology & Baselines

### 6.1 Three-Condition Comparative Evaluation
Every benchmark suite evaluates the agent across three explicit configurations using identical task fixtures:
1. **Stateless Agent (Baseline 1):** Operates with zero memory across steps and sessions.
2. **Retrieval-Only RAG Agent (Baseline 2):** Queries a keyword/vector store at step start and pastes raw chunks directly into context; no candidate validation, consolidation, or forgetting.
3. **Memory-Aware Harness (Condition 3):** Full lifecycle harness with four-tier memory, budgeted context manifest, quarantined writes, consolidation, and verifiable forgetting.

### 6.2 Standardized Benchmark Scenarios
- **Scenario A (Cross-Session Continuity):** Multi-session user preference and project state continuity across session restarts.
- **Scenario B (Crash Recovery & Idempotency):** Simulating process termination during tool dispatch; verifying safe rehydration without repeated side effects.
- **Scenario C (Privacy & Forgetting Compliance):** Measuring compliance with GDPR-style deletion requests; asserting 0% retrieval of purged information while preserving non-deleted memory.

### 6.3 Measured Metrics & Statistical Reporting
- **Task Success Rate:** Percentage of benchmark tasks achieved.
- **Stale Memory Selection Rate:** Target **0.0%**.
- **Forgetting Compliance Rate:** Target **0.0%** retrieval of deleted items.
- **Provenance Completeness:** Target **100.0%**.
- **Context Efficiency:** Percentage reduction in token bloat vs. Baseline 2.
- **Step Latency (p50, p95):** SQLite lifecycle overhead in milliseconds.
- **Reporting Rule:** Raw execution logs, variances, and negative results must be reported honestly. No claiming general superiority without empirical evidence.

---

## 7. Intermediate Milestones & Release Acceptance Criteria

### Milestone 1: v0.1.0 (Core Lifecycle MVP) — End of Phase 5
- **Scope:** Bounded FSM runtime, 4-tier memory models, unified SQLite WAL storage, multi-signal retrieval, context compiler, sandboxed tool runtime, trace exporter, and deterministic test doubles.
- **Acceptance Gate:**
  - Passes Ruff, strict Mypy, Pytest.
  - Package installs via `pip install .` and CLI smoke test passes.
  - Documented as an **early experimental milestone**, not production-ready.

### Milestone 2: v0.2.0 (Continuity & Recovery) — End of Phase 6
- **Scope:** Checkpoint snapshots, resume protocol, branch DAG, side-effect boundary audit, dirty rollback flags, and fallible compensation hooks.
- **Acceptance Gate:**
  - Crash-recovery matrix tests pass across all step boundaries.
  - Rollback past any mutating tools (`IDEMPOTENT_WRITE` or `NON_IDEMPOTENT_WRITE`) flags `PARTIAL_DIRTY_ROLLBACK` and never silently re-executes.
  - Branch visibility tests assert memory isolation across execution tree branches.

### Milestone 3: v0.3.0 (Governance & Replay) — End of Phase 8
- **Scope:** Candidate quarantine, episodic-to-semantic consolidation, lineage graphs, verifiable tombstone deletion, TTL expiry, and deterministic replay engine.
- **Acceptance Gate:**
  - 0.0% retrieval of deleted/expired items in forgetting compliance tests.
  - Invalidation of derived memories referencing purged records (`origin_status = 'ORIGIN_PURGED'`).
  - 100% bit-for-bit trajectory replay fidelity using recorded traces and test doubles.

### Milestone 4: v0.4.0 (Evaluation & Benchmarks) — End of Phase 10
- **Scope:** 3-baseline comparative evaluation runner, 3 frozen benchmark task suites, and automated statistical reporting.
- **Acceptance Gate:**
  - Benchmark suites execute completely offline without API keys.
  - Reports disclose raw logs, confidence intervals, and limitations.

### Milestone 5: v1.0.0 (Production Hardened Release) — End of Phase 12
- **Scope:** Hardened public API, backward compatibility migrations, security audit, 3 production demos, full documentation, packaging distribution, CI matrix across Python 3.11/3.12/3.13.
- **Acceptance Gate:**
  - 200–300 substantive tests passing with 100% green status.
  - Zero unresolved Critical or High findings in Claude Code independent review.
  - Demonstrable API stability and documented limitations.

---

## 8. Comprehensive Risk Register

| Risk ID | Category | Description | Severity | Likelihood | Mitigation Strategy |
|---|---|---|:---:|:---:|---|
| **RSK-01** | Architecture | Naive rollback accidentally repeats destructive external tool calls or assumes idempotent writes are reversible. | **Critical** | Medium | Separate internal state from external side effects; classify both idempotent and non-idempotent writes as external mutations; flag `PARTIAL_DIRTY_ROLLBACK`; execute fallible compensation. |
| **RSK-02** | Security | Memory retrieval injects adversarial instructions into prompt or breaks out of delimiters. | **High** | Medium | Structural untrusted content fencing (`<untrusted_memory>`); mandatory XML escaping of memory content (`&lt;`, `&gt;`, `&amp;`); memory trust categorization (`MemoryOrigin`). |
| **RSK-03** | Data Hygiene | Unbounded memory growth degrades retrieval quality and increases latency. | **High** | High | Phase 4 consolidation engine; LRU/decay pruning; TTL expiry; hard capacity thresholds. |
| **RSK-04** | Compliance | Deleted memories resurface in retrieval or persist in derived knowledge. | **Critical** | Low | SQL-level tombstone exclusion invariant; WAL frame truncation (`PRAGMA wal_checkpoint(TRUNCATE)`); lineage propagation to derived memories (`ORIGIN_PURGED`). |
| **RSK-05** | Reliability | Harness crashes mid-step leaving database or tool state corrupted. | **High** | Medium | SQLite WAL transactions; crash-safe two-phase step commit protocol (intent registration $\rightarrow$ dispatch $\rightarrow$ outcome update $\rightarrow$ atomic commit); `OUTCOME_UNKNOWN` handling on recovery. |
| **RSK-06** | Testing | Test suite flakiness due to non-deterministic external LLMs or APIs. | **High** | High | First-class deterministic test doubles (`FakeModelAdapter`, `DeterministicClock`, `SeedableRNG`); zero live API calls in CI. |
| **RSK-07** | Scope | Project scope expands into distributed multi-agent swarm framework. | **Medium** | Medium | Strict adherence to Non-Goals in [SPEC.md](file:///Users/apple/Projects/memory-aware-agent-harness/SPEC.md); single-agent, single-process focus. |
| **RSK-08** | Packaging | Packaging deferred to project end results in last-minute dependency conflicts. | **Medium** | High | Introduce minimal `pyproject.toml`, linters, and smoke CI in Phase 0 (Task 0.9). |

---

## 9. Pre-Implementation Decision Lock Checklist

Before closing Phase 0 and initiating Phase 1 implementation commits, the following must be formally verified and approved:

- [ ] Core thesis and empirical hypothesis accepted ([SPEC.md](file:///Users/apple/Projects/memory-aware-agent-harness/SPEC.md)).
- [ ] Four behavioral memory categories in unified SQLite schema accepted.
- [ ] Explicit Run States vs. Step Operations FSM accepted.
- [ ] Distinguish runtime rollback from fallible external compensation accepted.
- [ ] 13-phase roadmap with 340-commit target cadence accepted ([ROADMAP.md](file:///Users/apple/Projects/memory-aware-agent-harness/ROADMAP.md)).
- [ ] Accounting of 3 existing commits + 13 remaining Phase 0 tasks accepted.
- [ ] Strict quality gates (Python 3.11+, strict mypy, ruff, 200–300 tests) accepted.
- [ ] Zero-API-key testing and benchmark requirement accepted.
- [ ] Intermediate release gates (`v0.1.0` to `v1.0.0`) accepted.
- [ ] 5-stage collaborative workflow (ChatGPT / Antigravity / Claude Code / CI) accepted.
- [ ] Architecture documents jointly approved and locked.
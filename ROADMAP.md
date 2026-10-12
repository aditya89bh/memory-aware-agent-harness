# ROADMAP.md: 13-Phase Master Development Roadmap

**Status:** REVISED (Phase 0 Independent Review Remediation Round 2)  
**Governing Rule:** No application or runtime implementation until `SPEC.md`, `ARCHITECTURE.md`, and `ROADMAP.md` are jointly approved and locked.  
**Planning Target:** 340 meaningful commits across 13 phases (agreed planning range: 300–350 commits). *Target, not an artificial quota.*

---

## 1. Master Commit Allocation & Milestones

| Phase | Focus Area | Target Commits | Cumulative Commits | Milestone / Release Gate |
|---|---|:---:|:---:|---|
| **Phase 0** | Foundation, Specs, Package Setup & Remediation | 31 | 31 | Project skeleton, typed contracts, & approved specs |
| **Phase 1** | Core Runtime & Lifecycle | 28 | 59 | FSM execution & budget bounds |
| **Phase 2** | Memory Model & Storage Abstractions | 32 | 91 | Unified SQLite WAL persistence |
| **Phase 3** | Retrieval, Ranking, Context Compiler | 32 | 123 | Multi-signal ranker & fenced context |
| **Phase 4** | Memory Writes, Consolidation, Forgetting | 28 | 151 | Quarantined writes & verifiable deletion |
| **Phase 5** | Tool Runtime & Routing | 25 | 176 | **v0.1.0 (Core Lifecycle MVP)** |
| **Phase 6** | Checkpointing, Resume, Rollback, Branching | 25 | 201 | **v0.2.0 (Continuity & Recovery)** |
| **Phase 7** | Tracing, Provenance, Event Model | 26 | 227 | Lineage graph & PII redaction |
| **Phase 8** | Replay & Deterministic Hooks | 24 | 251 | **v0.3.0 (Governance & Replay)** |
| **Phase 9** | Evaluation Framework & Metrics | 25 | 276 | 3-baseline comparison runner |
| **Phase 10** | Benchmarks & Regression Suites | 22 | 298 | **v0.4.0 (Evaluation & Benchmarks)** |
| **Phase 11** | Integrations & Realistic Demos | 20 | 318 | 3 runnable end-to-end scenarios |
| **Phase 12** | Hardening, Docs, Packaging, Release | 22 | 340 | **v1.0.0 (Production Hardened)** |
| **Total** | | **340** | | |

---

## 2. Accounting of Phase 0 Commits

### 2.1 Baseline Phase 0 Commits (Commits 1 to 17)
The following 17 baseline commits established the Phase 0 foundation up to published commit `66481a9`:
1. `9535535`: `docs: add draft spec specification`
2. `45b66c8`: `docs: add draft architecture specification`
3. `96d59df`: `docs: add draft roadmap specification`
4. `78528bc`: `docs(spec): reconcile execution FSM, unified storage, and retention boundaries`
5. `a98153b`: `docs(architecture): specify deep immutability, fallible compensation, and SQLite WAL schema`
6. `0cf28f0`: `docs(roadmap): expand 13-phase master plan with commit allocation and release gates`
7. `9836f57`: `docs(adr): add architecture decision records ADR-0001 through ADR-010`
8. `837b8cb`: `docs(security): establish security architecture, threat model, and untrusted boundaries`
9. `818bdf2`: `build: create initial pyproject.toml and project metadata`
10. `d82d327`: `chore(lint): configure ruff linting, formatting, and pre-commit hooks`
11. `9752316`: `chore(types): configure strict mypy static type checking`
12. `e69b19e`: `init(pkg): create package skeleton and version declaration`
13. `6001e12`: `feat(contracts): define public types, immutable data models, exceptions, and ports`
14. `5c39352`: `test(fixtures): establish test directory structure, fixtures, and contracts smoke tests`
15. `816f696`: `ci: add github actions workflow for linting, typing, testing, and packaging`
16. `dd93549`: `build(verify): validate package build, packaging smoke tests, and Phase 0 report`
17. `66481a9`: `ci(fix): scope ruff checks to src and tests and synchronize architecture code block`

### 2.2 Independent Review Remediation Round 1 Commits (Commits 18 to 28)
Addressing Claude Code independent architecture review findings (H-01 through H-07, and Medium findings):
18. `fa11fd1`: `fix(contracts): add NewType identifiers, CandidateStatus, and validation rules` (M-01 through M-05)
19. `608046a`: `feat(contracts): implement deep immutability, normalization, and serialization` (H-01 initial attempt)
20. `5a85a4d`: `feat(contracts): define FSM states, transition matrix, and execution contexts` (H-02, H-03, H-04)
21. `3d02e60`: `feat(replay): add deterministic test doubles for clock and id generator` (H-03 deterministic doubles)
22. `7e7457d`: `docs(spec): reconcile rollback semantics, erasure propagation, and threat model` (H-05, H-06, H-07)
23. `4577952`: `docs(architecture): update FSM, step commit protocol, and prune duplicate code` (H-02, H-04, H-05, M-06)
24. `1a27416`: `docs(roadmap): update Phase 0 remediation accounting and milestone gates` (M-11)
25. `cb4c4da`: `chore(license): add Apache-2.0 license and fix packaging metadata` (M-07)
26. `98b414f`: `chore(ci): align ruff configuration and add wheel installation smoke test` (M-08, M-09)
27. `dcf1ebe`: `test(remediation): add comprehensive negative-path and failure test suite` (M-10)
28. `8deb860`: `docs(report): compile independent review remediation matrix and validation evidence` (Round 1 report)

### 2.3 Independent Review Remediation Round 2 Commits (Commits 29 to 31)
Targeted remediation following independent reproduction of H-01, H-03, and N-01:
29. `a2b751b`: `fix(contracts): replace MappingProxyType with FrozenDict and harden serialization` (H-01 complete fix: hash, deepcopy, pickle, asdict, UTC equivalence, NaN/Inf and invalid metadata rejection)
30. `12d05b6`: `feat(contracts): require execution contexts, integrate token usage and failure details` (H-03 complete fix: mandatory contexts, cancellation/deadline semantics, TokenUsage/FailureDetails integration, structured exception fields)
31. `HEAD`: `docs(remediation): reconcile git commit history and audit matrix for review round 2` (N-01 complete fix: git log synchronization, finding classifications, roadmap 300–350 reconciliation)

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
# ROADMAP.md

**Status:** PROPOSED, not approved  
**Governing rule:** no implementation until SPEC.md, ARCHITECTURE.md and ROADMAP.md are approved together.

## Phase 0. Contract and evidence design

**Deliverables:** approved scope and ADRs; lifecycle/state diagram; contracts for Run, Step, Memory, Candidate, ContextManifest, ToolInvocation, TraceEvent; threat model; sample cross-session tasks and baselines; documented acceptance tests.  
**Exit gate:** no contradictory requirements; each v0.1 goal has at least one testable acceptance criterion.  
**No production code until the Phase 0 design is accepted.**

## Phase 1. Minimal runtime skeleton (v0.1)

Implement bounded single-agent orchestrator, explicit run states, budgets, typed provider interfaces, fake deterministic model and fake tools.  
**Exit gate:** deterministic multi-step run plus failure and budget tests; no memory involved yet.

## Phase 2. Memory retrieval and context construction (v0.1)

Implement SQLite memory schema, namespace isolation, keyword/exact retrieval, transparent ranking, context budgets, provenance manifests and retrieval traces.  
**Exit gate:** earlier-session fact used in later-session context; no cross-namespace leakage; over-budget content excluded with reasons.

## Phase 3. Memory writes and tool execution (v0.1)

Implement tool allowlist, validated arguments, side-effect classification, candidate extraction, memory write policy, persistence and acceptance/rejection traces.  
**Exit gate:** only eligible candidates persist; tools cannot bypass dispatcher; rejected memories have reasons; failure cases are tested.

## Phase 4. Traceable MVP and baselines (v0.1 release)

Add trace export, example walkthrough, no-memory/retrieval-only configurations, documentation, CLI/API quickstart and CI.  
**Exit gate:** fresh clone passes lint/type/test/build; offline demonstration shows all three modes; any observed improvement is reported as dataset-specific. Tag v0.1.0 only after review.

## Phase 5. Checkpoint and safe recovery (v0.2)

Add safe checkpoints, crash recovery, status reconciliation, idempotency metadata and documented unknown-outcome behavior.  
**Exit gate:** tested crashes around tool dispatch; non-idempotent side effects are never silently repeated.

## Phase 6. Replay hooks (v0.2)

Add recorded replay and event/state consistency checks with fake providers, with versioned schemas.  
**Exit gate:** deterministic replay of recorded artifacts; clearly distinguish simulated/recorded replay from re-running live providers. Tag v0.2.0.

## Phase 7. Consolidation and supersession (v0.3)

Add duplicate detection, policy-driven merges/supersession, source lineage, stale-memory handling.  
**Exit gate:** source lineage is preserved and changed facts do not coexist as equally active without explicit conflict status.

## Phase 8. Forgetting and retention (v0.3)

Add expiry, deletion/tombstones and retention rules for memory, trace and checkpoint records.  
**Exit gate:** deleted/expired memory is never retrieved; retention semantics and data limitations are tested and documented. Tag v0.3.0.

## Phase 9. Evaluation framework and provider adapters (v0.4)

Build fixed multi-session tasks, leakage/adversarial/staleness scenarios, no-memory and retrieval-only baselines, uncertainty/overhead reporting; introduce optional provider adapters.  
**Exit gate:** reproducible comparative report, raw outputs, failure cases, and limitations. No unsupported claim of general superiority. Tag v0.4.0.

## Phase 10. Hardening and public release (v1.0)

Stabilize interfaces, migration/compatibility testing, documentation, examples, release automation and security review.  
**Exit gate:** all CI passes on supported Python versions, documented limitations, verified versioned artifact, release notes, final architecture review. Tag v1.0.0 if evidence warrants.

## Execution workflow per phase

1. **ChatGPT:** scope the phase, update acceptance criteria and architectural decisions.
2. **Antigravity CLI:** implement one coherent task per commit, add tests and notes; do not optimize for commit volume.
3. **Claude Code:** perform independent correctness, architecture, security and maintainability review, with ranked findings.
4. **Antigravity CLI:** fix verified findings and add regression tests.
5. **GitHub CI:** run checks and confirm green status on the pushed commit. Phase closes only after evidence is recorded.

## Quality gates

Every phase: clean branch status, passing lint and formatting checks, static type checks, unit/integration/failure tests relevant to scope, package build if applicable, documentation consistent with behavior, no secrets or unredacted sensitive test data, and a concise phase report including known limitations.

Suggested Python toolchain (subject to approval): Python 3.11+, `pytest`, `ruff`, `mypy`, `build`, and SQLite via standard library. This is a preference, not a locked dependency list.

## Decision log before lock

- [ ] Thesis and empirical claim accepted.
- [ ] Non-goals accepted.
- [ ] Python and SQLite choices accepted.
- [ ] Single-agent synchronous MVP accepted.
- [ ] Memory safety/write policy accepted.
- [ ] Side-effect/replay guarantees accepted.
- [ ] v0.1 exit gate accepted.
- [ ] Docs jointly approved and status changed to LOCKED.
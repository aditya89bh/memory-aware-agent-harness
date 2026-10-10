# SPEC.md

**Project:** memory-aware-agent-harness  
**Portfolio layer:** Runtime Infrastructure  
**Status:** PROPOSED, not approved  
**Specification version:** 0.1-draft

## 1. Thesis

Agent memory belongs inside the execution lifecycle. A harness should decide **when** to read memory, **what** can enter context, **when** to write candidate memories, and **how** those decisions are traced, recovered, and evaluated. Merely attaching a vector store to an agent is insufficient.

**Testable hypothesis:** On controlled, multi-session agent tasks, lifecycle-integrated memory improves task success and continuity over (A) no memory and (B) retrieval-only memory, without unacceptable penalties in latency, cost, stale-memory use, or unsafe retention. This is a hypothesis to evaluate, not a promised outcome.

## 2. Intended users and positioning

Primary users: engineers building or studying single-agent systems that execute tools over multiple sessions. The project is a **small, inspectable Python runtime** with memory lifecycle primitives and observable decisions, not an application, chatbot UI, or universal agent framework.

## 3. Goals

1. Put retrieval and context selection before agent/model decisions.
2. Capture candidate memories from completed steps and tool observations according to explicit policies.
3. Make runs, memory decisions, context composition, and tool execution inspectable.
4. Persist checkpoints and support safe recovery, with explicit limitations for side effects.
5. Add consolidation and forgetting as policy-driven lifecycle operations.
6. Expose replay and evaluation hooks to compare policies and baselines.
7. Provide deterministic, offline tests and illustrative examples.

## 4. Non-goals

- Replacing LangGraph, AutoGen, CrewAI, or other orchestration stacks.
- General multi-agent orchestration, distributed scheduling, or production-scale queues.
- Training or fine-tuning models; claiming human-like or autobiographical memory.
- Building an embedding model, vector database, or memory knowledge graph.
- Guaranteeing factual truth, perfect recall, or automatic memory correctness.
- Guaranteeing exactly-once external tool effects or deterministic replay of live LLM calls.
- A hosted service, frontend dashboard, or SaaS authentication/billing platform.
- Fully automatic deletion from third-party systems not under harness control.
- Storing raw secrets, credentials, or sensitive personal information by default.

## 5. Design principles

- **Explicit lifecycle:** memory operations are named execution steps, not hidden prompts.
- **Policy before provider:** ranking, retention, and write criteria are configurable and inspectable.
- **Safe defaults:** no silent promotion of untrusted tool output into permanent memory.
- **Small core:** single agent, one process, local durable storage first.
- **Provenance:** every accepted memory identifies its source and creation decision.
- **Observability:** trace both actions taken and decisions rejected, when practical.
- **Reproducibility:** frozen fixtures, seeded fake models/tools, versioned policies.
- **Honest recovery:** identify what can and cannot be safely replayed.

## 6. Release scopes

### v0.1: Prove the lifecycle

Required:
- One synchronous single-agent step-loop with configurable max steps and budgets.
- Typed ports for model, tool, memory store, retrieval strategy, context builder, and hooks.
- Memory retrieval before a model decision; bounded, provenance-labeled context assembly.
- Explicit post-step candidate extraction, validation, and write/reject decisions.
- Local SQLite persistence for memories and run event records.
- JSONL event export and readable traces.
- A deterministic example requiring information from an earlier session.
- Baselines: no-memory and retrieval-only configurations using the same model/tool fixtures.
- Unit/integration tests, CI, reference docs, and a minimal CLI or Python entrypoint.

Out of scope for v0.1: checkpoint resume, consolidation, automatic forgetting, LLM-based summarization, embedding/vector retrieval, and production remote integrations. The ports and event schema must leave room for them without implementing them prematurely.

### v0.2: Recovery and replay

Checkpoint after defined safe boundaries; resume from persisted state; replay recorded decisions with fake providers. Mutating tools require explicit idempotency keys or an operator-mediated recovery decision. Replay is faithful to recorded inputs and decisions, not a promise that external systems recreate prior results.

### v0.3: Memory governance

Consolidation, supersession, expiry, explicit forgetting, deletion/tombstones, and policy versioning. Track lineage between source memories and consolidated memories. Define retention limits for events and checkpoints separately.

### v0.4: Evaluation and adapters

Comparative benchmark harness, ablations, leakage/staleness tests, cost/latency reporting, and optional model/tool adapters. External providers are plug-ins, not core dependencies.

### v1.0: Stabilization

Stable public interfaces, migration-tested persistence, security review, reproducible benchmarks, comprehensive docs, and tagged release. Release is gated by evidence, not number of commits.

## 7. Functional requirements

| ID | Requirement | First release | Verification |
|---|---|---|---|
| FR-01 | Execute bounded single-agent runs with explicit states | v0.1 | Deterministic end-to-end test |
| FR-02 | Retrieve memories before model action with namespace isolation | v0.1 | Cross-namespace isolation test |
| FR-03 | Construct context within token/character budget, citing source IDs | v0.1 | Budget and provenance tests |
| FR-04 | Apply explicit write policy and reject ineligible candidates | v0.1 | Allow/deny policy tests |
| FR-05 | Record memory reads, selected/excluded items, tool calls, writes, failures | v0.1 | Event trace assertions |
| FR-06 | Persist memory and run event history in SQLite | v0.1 | Restart/persistence test |
| FR-07 | Call tools through registered allowlisted names and typed arguments | v0.1 | Unknown/invalid tool tests |
| FR-08 | Resume from safe checkpoints without silently re-running side effects | v0.2 | Crash matrix tests |
| FR-09 | Reconstruct a run from recorded decisions and events | v0.2 | Replay consistency tests |
| FR-10 | Consolidate memories while preserving lineage | v0.3 | Lineage consistency tests |
| FR-11 | Forget/expire memories so they no longer appear in retrieval | v0.3 | Deletion and expiry tests |
| FR-12 | Compare policies against baselines on fixed tasks | v0.4 | Benchmark CI/test harness |

## 8. Core data contracts (conceptual)

- **Run:** `run_id`, `session_id`, `namespace_id`, status, timestamps, policy versions, step count, budget and optional checkpoint pointer.
- **RunStep:** `step_id`, input references, retrieved IDs, context manifest, model decision, tool interaction references, result and status.
- **MemoryRecord:** stable `memory_id`, `namespace_id`, content, memory kind, source references, confidence/quality metadata when available, created/updated timestamps, optional expiry, version and state (`active`, `superseded`, `deleted`).
- **MemoryCandidate:** content, source event IDs, proposed kind, rationale, validation result and write decision; not yet trusted long-term memory.
- **ContextManifest:** ordered sections, selected memory IDs and versions, excluded IDs with reasons, estimated size, policy version.
- **ToolInvocation:** call ID, tool name, validated args, side-effect classification (`read_only`, `idempotent`, `non_idempotent`), idempotency key where applicable, state, redacted result/error.
- **TraceEvent:** event ID, run/step IDs, sequence, timestamp, event type, outcome, redacted metadata and schema version.
- **Checkpoint:** run state and references to durable completed results, with schema and policy versions (v0.2).

**Identity rule:** `namespace_id` scopes retrieval, writes, replay, and deletion. A namespace can represent a user, tenant, or sandbox; there is no cross-namespace read by default.

## 9. Step execution contract

`START -> RETRIEVE -> BUILD_CONTEXT -> DECIDE -> [TOOL_EXECUTE] -> OBSERVE -> PROPOSE_MEMORY -> VALIDATE/WRITE -> TRACE -> NEXT_STEP or END`

- Each transition emits a structured event.
- Failures become explicit terminal or recoverable states, never silently skipped.
- Model-visible retrieved material is labeled untrusted; it cannot override tool policy or system instructions.
- A candidate memory is written only if it passes the configured policy; otherwise the rejection reason is recorded.
- Memory contents never directly authorize tool calls.
- No tool executes outside the central registry/dispatcher.

## 10. Memory semantics

- v0.1 supports exact/keyword retrieval over stored text and optional memory kind filters. No embeddings are required.
- Use recency and relevance as transparent configurable signals, without claiming universal optimal ranking.
- Write policy filters empty, duplicative, unsafe, or unsupported candidates. Default: store only explicitly permitted task facts/preferences from approved sources; no wholesale transcript capture.
- Store provenance and timestamps; distinguish observed facts from model-derived interpretations.
- v0.3 adds supersession (replace outdated facts), consolidation (merge with lineage), expiry and forgetting (exclude deleted/expired records from all future context).

## 11. Safety and reliability constraints

- Fail closed on invalid tool name, schema, policy, or namespace.
- Structured tool arguments must be validated before dispatch.
- Treat model outputs and retrieved/tool text as untrusted; protect instructions from prompt injection.
- Redact credentials and configured sensitive fields from logs and traces.
- Bound steps, tool calls, memory items, context size, wall-clock time and optional cost.
- Avoid storing raw chain-of-thought or private internal model reasoning.
- Explicitly mark unknown outcomes of non-idempotent tools after crashes; require reconciliation.
- SQLite is a local reference backend, not a production distributed consistency guarantee.

## 12. Quantitative evaluation (targets, not results)

Primary: task success on fixed multi-session tasks versus no-memory and retrieval-only baselines.  
Secondary: retrieval precision@k, stale-memory selection rate, provenance coverage, write precision, forgetting compliance, replay consistency, mean/p95 latency and token/cost overhead.

Proposed acceptance gates:
- All required deterministic lifecycle tests pass on a fresh environment.
- 100% of accepted memories have source provenance; 100% of tool calls have trace IDs.
- Zero cross-namespace retrieval in isolation tests.
- Zero deleted/expired items returned in relevant v0.3 tests.
- Comparison report includes task counts, uncertainties, raw outputs, and negative results.
- No claimed improvement based exclusively on self-generated examples; benchmark fixtures and evaluator design must disclose limitations.

No numeric success uplift is promised before evaluation.

## 13. Definition of done per phase

Each phase requires: documented contracts, focused tests, negative/failure tests, repeatable example, passing lint/type/test/build checks, reviewed migrations when storage changes, and a succinct report identifying limitations. Commits count is not a quality criterion.

## 14. Open decisions for approval

1. Python 3.11+ as the reference language.
2. SQLite as primary local persistence and JSONL as export.
3. Single-agent synchronous v0.1 with explicit optional provider ports.
4. v0.1 keyword/exact retrieval rather than vector dependencies.
5. Memory lifecycle integration, not a new general orchestration framework, as the differentiating claim.
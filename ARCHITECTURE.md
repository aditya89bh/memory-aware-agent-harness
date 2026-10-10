# ARCHITECTURE.md

**Status:** PROPOSED, not approved  
**Architecture:** modular monolith, one-process Python runtime; ports-and-adapters boundaries

## 1. Context and architectural choice

This repository implements a narrow agent execution engine where memory decisions occur inside each run/step. The core is independent of specific LLM vendors, vector stores, and agent frameworks. Build a reference implementation rather than an integration-heavy abstraction library.

## 2. System overview

```mermaid
flowchart TD
    A[Run API / CLI] --> R[Run Orchestrator and State Machine]
    R --> MR[Memory Retrieval Policy]
    MR --> MS[(Memory Store: SQLite)]
    MR --> CB[Context Builder]
    CB --> MP[Model Port]
    MP --> R
    R --> TD[Tool Dispatcher + Registry]
    TD --> TP[Tool Adapters]
    TP --> R
    R --> MW[Candidate Extractor + Write Policy]
    MW --> MS
    R --> EB[Event Recorder]
    EB --> ES[(Run/Event Store: SQLite)]
    ES --> EX[JSONL Trace Export]
    ES -. v0.2 .-> CP[Checkpoint/Replay]
    MS -. v0.3 .-> GOV[Consolidate/Forget]
    ES -. v0.4 .-> EVAL[Evaluation/Ablations]
```

## 3. Module responsibilities

| Module | Owns | Does not own |
|---|---|---|
| `runtime/` | Run lifecycle, state transitions, budgets, errors | Provider-specific API calls |
| `memory/` | Retrieval, candidate generation, write policy, memory contracts | Final model prompts/tool authorization |
| `context/` | Budgeted assembly, untrusted content boundaries, context manifest | Memory persistence |
| `models/` | Model port, deterministic fake and optional adapters | Orchestration |
| `tools/` | Registry, schema validation, side-effect classifications, dispatch | Direct memory store access |
| `storage/` | SQLite adapters, transactions, migrations, JSONL export | Policies |
| `telemetry/` | Structured events, redaction, trace querying | Raw secrets |
| `checkpoint/` (v0.2) | Safe snapshot, recovery protocol, recorded replay | Re-running live side effects blindly |
| `governance/` (v0.3) | Retention, expiry, supersession, consolidation, deletion | External data deletion guarantees |
| `evaluation/` (v0.4) | Tasks, baselines, metrics, reproducibility report | Inflated benchmark claims |

## 4. Trust boundaries

1. Tool outputs, user content, retrieved records, and model responses are **untrusted inputs**.
2. The system/runtime policy, tool registry, and explicit caller authorization determine what may execute.
3. Tool results may become **candidate** memories, never unreviewed permanent facts.
4. The namespace is mandatory at every read and write boundary; the storage adapter also enforces it.
5. Redacted trace metadata may be exported; raw secrets and hidden model reasoning are excluded.

## 5. Lifecycle and order of operations

A new run is initialized with namespace, session, policies, tools, model port, and budgets. For each step:

1. Emit `step.started`.
2. Issue memory retrieval query based on current task and allowed context.
3. Rank/filter results and record selected and rejected item IDs with reasons.
4. Build bounded context with explicit provenance/untrusted labels.
5. Ask model port for a structured decision: finish or request registered tool.
6. Validate tool and arguments before dispatch; classify side effects.
7. Execute (if requested) and capture a redacted observation/error.
8. Produce candidate memories from allowed observations and step outputs.
9. Validate and write/reject each candidate according to policy.
10. Emit final events and repeat within limits, or complete/fail the run.

**Important v0.1 simplification:** a fake model and fixtures are first-class. A real LLM adapter is optional and must not be required for CI.

## 6. Consistency and transactional boundaries

- Memory records and event rows reside in SQLite; use transactions for operations that should commit atomically in the local store (e.g., memory write plus its acceptance event).
- External tool effects cannot share the SQLite transaction. Record an invocation intent before dispatch and its outcome afterward.
- A crash between external effect and outcome persistence can leave `outcome_unknown`; do **not** retry a non-idempotent tool automatically.
- Checkpoint recovery (v0.2) can skip completed, durably recorded steps and re-run only safe operations or require reconciliation.
- Use monotonic per-run event sequence numbers; timestamps are descriptive, not the source of ordering truth.

## 7. Data model and event types

Primary tables envisioned: `runs`, `steps`, `trace_events`, `memories`, `memory_provenance`, `tool_invocations`, `schema_migrations`; later `checkpoints`, `memory_lineage`, `tombstones`.

Core event taxonomy: `run.started`, `step.started`, `memory.retrieved`, `context.built`, `model.decided`, `tool.invoked`, `tool.completed`, `tool.failed`, `memory.proposed`, `memory.accepted`, `memory.rejected`, `step.completed`, `run.completed`, `run.failed`. v0.2+ adds `checkpoint.created`, `run.resumed`, `replay.started/completed`; v0.3 adds `memory.superseded`, `memory.consolidated`, `memory.expired`, `memory.deleted`.

Events carry schema versions and correlation IDs. Payloads should contain references and redacted summaries, not indiscriminate raw messages.

## 8. Contracts and dependency rules

- Core ports use Python `Protocol` interfaces or equivalent typed abstractions.
- Dependencies point inward: adapters depend on domain contracts, not the reverse.
- Orchestrator calls memory/context/model/tools via interfaces, not private module internals.
- Do not let model adapters write directly to persistence.
- Do not let tools escalate authority based on retrieved content.
- Keep public API small and versioned; `internal` modules need no stability promise until v1.0.
- Use dependency injection for deterministic testing.

## 9. Proposed repository layout

```text
memory-aware-agent-harness/
├── README.md
├── SPEC.md
├── ARCHITECTURE.md
├── ROADMAP.md
├── pyproject.toml                 # implementation only after approval
├── src/memory_aware_agent_harness/
│   ├── __init__.py
│   ├── api.py
│   ├── contracts.py
│   ├── runtime/
│   │   ├── orchestrator.py
│   │   ├── state.py
│   │   └── budgets.py
│   ├── memory/
│   │   ├── models.py
│   │   ├── retrieval.py
│   │   ├── candidates.py
│   │   └── policies.py
│   ├── context/
│   │   └── builder.py
│   ├── models/
│   │   ├── protocol.py
│   │   └── fake.py
│   ├── tools/
│   │   ├── registry.py
│   │   └── dispatcher.py
│   ├── storage/
│   │   ├── sqlite.py
│   │   └── migrations/
│   ├── telemetry/
│   │   ├── events.py
│   │   └── export.py
│   ├── checkpoint/              # v0.2
│   ├── governance/              # v0.3
│   └── evaluation/              # v0.4
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── failure/
│   └── fixtures/
├── examples/
│   └── cross_session_continuity/
├── benchmarks/                  # v0.4
├── docs/
│   ├── lifecycle.md
│   ├── memory_policy.md
│   ├── security.md
│   └── limitations.md
└── .github/workflows/
    └── ci.yml
```

Directories for future releases are planned, not requirements to create empty packages on day one.

## 10. Test strategy

- **Unit:** policy ranking, candidate validation, token/size budget, state transitions, registry validation.
- **Integration:** multi-session continuity, SQLite restart, trace/event correlations, namespace isolation.
- **Failure:** model timeout, invalid JSON/decision, malicious retrieved text, tool crash, DB failure, duplicated event/write attempts, budget exhaustion.
- **v0.2:** crash at every durable boundary, replay fidelity, side-effect `outcome_unknown` handling.
- **v0.3:** deletion visibility, expiry, supersession and consolidation lineage.
- **v0.4:** frozen multi-session task suite and baseline ablations; no dependence on paid APIs in CI.

## 11. Key architecture decision records to capture

- ADR-001: modular monolith and ports/adapters rather than framework extension.
- ADR-002: single-agent synchronous v0.1; bounded explicit state machine.
- ADR-003: SQLite first, stable persistence ports.
- ADR-004: keyword/exact retrieval before embeddings.
- ADR-005: candidate-to-policy-to-write rather than automatic transcript storage.
- ADR-006: side-effect-aware checkpoint and replay semantics.

Each ADR must state context, alternatives, decision, consequences and reversal trigger.
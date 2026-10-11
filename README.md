# memory-aware-agent-harness

A production-oriented, modular, single-agent execution harness in Python where memory participates in execution decisions rather than existing only as an external retrieval service.

## Core Thesis

Agent memory belongs inside the execution lifecycle. The harness governs:
- **Retrieval:** Pre-decision, policy-ranked memory retrieval with strict namespace isolation.
- **Context Compilation:** Budget-capped, provenance-labeled context assembly with untrusted content fencing.
- **Candidate Lifecycle:** Explicit extraction, policy validation, write-authorization, and rejection tracking.
- **Governance:** Background and post-run consolidation, supersession graphs, and verifiable forgetting.
- **Continuity & Replay:** Durable checkpoints, branching, replay hooks, and an explicit boundary separating internal state rollback from irreversible external side effects.

## Documentation Index

- [SPEC.md](SPEC.md): Complete functional specification and requirements.
- [ARCHITECTURE.md](ARCHITECTURE.md): Architecture model, public API contracts, and SQLite schema.
- [ROADMAP.md](ROADMAP.md): 13-phase master development plan (340 target commits).
- [docs/adr/](docs/adr/): Architecture Decision Records (ADR-0001 through ADR-0010).
- [docs/security.md](docs/security.md): Security architecture, threat model, and untrusted boundaries.

## Status

Currently in **Phase 0: Foundation, Specs, Package Setup**.
No runtime implementation code has started.

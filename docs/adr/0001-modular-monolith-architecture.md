# ADR-0001: Modular Monolith Architecture with Ports and Adapters

## Status
Accepted

## Context
AI agent frameworks frequently adopt distributed microservices, network actor models, or heavy multi-agent orchestration layers. While appropriate for multi-agent swarms, distributed architectures introduce significant RPC latency, non-deterministic network failure modes, complex serialization, and operational overhead. The goal of `memory-aware-agent-harness` is to investigate and prove the thesis that memory must be a first-class component of the single-agent execution lifecycle.

## Decision
We implement a single-process, local-first **Modular Monolith** using **Ports-and-Adapters (Hexagonal Architecture)** in Python 3.11+.
1. The domain core (runtime FSM, memory models, context compilation, ranking, governance) has zero dependencies on external frameworks, databases, or LLM SDKs.
2. External interactions (SQLite, file systems, model APIs, external tools) connect through typed Python `Protocol` ports.
3. Modules communicate via clear in-memory interfaces, enabling fast, synchronous, deterministic execution.

## Consequences
- **Positive:** Sub-millisecond execution overhead; straightforward local inspection; zero external network dependencies for testing; deterministic test doubles can replace adapters seamlessly.
- **Negative:** Single-process execution model limits scalability to single-node instances.
- **Reversal Trigger:** If multi-agent distributed scheduling or cross-node agent collaboration becomes an explicit project requirement.

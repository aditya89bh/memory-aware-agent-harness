# ADR-0002: Unified SQLite Table for Four Behavioral Memory Categories

## Status
Accepted

## Context
The harness defines four cognitive memory categories: Working (ephemeral scratchpad), Episodic (chronological interactions), Semantic (distilled facts), and Procedural (executable skills). An initial design considered creating four distinct database tables (`working_memories`, `episodic_memories`, `semantic_memories`, `procedural_memories`). However, this fragmentation creates redundant schema maintenance, complicates cross-category retrieval and transactions, and causes migration churn without providing query performance advantages.

## Decision
We store all memory categories in a **single unified `memory_records` table** in SQLite, with an indexed `kind` discriminator column (`'working'`, `'episodic'`, `'semantic'`, `'procedural'`).
1. A composite partial index `idx_memories_active ON memory_records(namespace_id, kind, state) WHERE state = 'active'` optimizes active-memory queries.
2. Distinct Python data models and validation contracts govern each tier at the domain layer before database insertion.

## Consequences
- **Positive:** Single atomic transaction boundary for multi-tier updates; simplified queries and schema migrations; zero table sprawl.
- **Negative:** Column structure must accommodate metadata fields shared across categories.
- **Reversal Trigger:** If demonstrable table lock contention or divergent storage requirements (e.g. specialized binary blob storage for procedural recipes) necessitate schema separation.

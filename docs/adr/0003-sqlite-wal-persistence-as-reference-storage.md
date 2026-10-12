# ADR-0003: SQLite WAL Persistence as Local Reference Storage

## Status
Accepted

## Context
A durable, inspectable persistence layer is required for runs, steps, memory records, tool invocations, checkpoints, and event logs. The storage backend must be local-first, zero-configuration, and fully ACID-compliant without requiring the user to install or manage an external database service (such as PostgreSQL or Redis).

## Decision
We select **SQLite in Write-Ahead Logging (WAL) mode** as the reference storage backend, utilizing Python's built-in `sqlite3` module.
1. `PRAGMA journal_mode = WAL;` enables concurrent readers while a writer commits.
2. `PRAGMA synchronous = NORMAL;` provides application-crash resilience in WAL mode (committed transactions survive unexpected process termination). While un-fsynced WAL frames could theoretically be lost on operating system crashes or sudden host power loss, mission-critical environments can configure `synchronous = FULL` or `EXTRA` via storage configuration.
3. `PRAGMA foreign_keys = ON;` enforces referential integrity across runs, steps, lineage, and invocations.
4. `PRAGMA secure_delete = ON;` zeroes out deleted content in database pages to prevent data remnants from persisting in unallocated pages.
5. Strict tenant isolation is enforced through relational parameterized queries (`WHERE namespace_id = ?`) backed by composite indexes, avoiding speculative cryptographic claims.

## Consequences
- **Positive:** Zero external setup; embedded in Python standard library; portable `.db` file for easy inspection, debugging, and backup; resilient to application crashes; zero page-leakage deletion.
- **Negative:** Concurrency is limited to a single writer process at a time; `synchronous = NORMAL` trades absolute power-loss durability for write throughput (mitigated by configuring `FULL` when required).
- **Reversal Trigger:** If multi-node distributed concurrency or remote client connections become required.

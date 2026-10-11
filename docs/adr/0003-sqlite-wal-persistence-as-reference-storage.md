# ADR-0003: SQLite WAL Persistence as Local Reference Storage

## Status
Accepted

## Context
A durable, inspectable persistence layer is required for runs, steps, memory records, tool invocations, checkpoints, and event logs. The storage backend must be local-first, zero-configuration, and fully ACID-compliant without requiring the user to install or manage an external database service (such as PostgreSQL or Redis).

## Decision
We select **SQLite in Write-Ahead Logging (WAL) mode** as the reference storage backend, utilizing Python's built-in `sqlite3` module.
1. `PRAGMA journal_mode = WAL;` enables concurrent readers while a writer commits.
2. `PRAGMA synchronous = NORMAL;` provides strong durability without excessive fsync latency.
3. `PRAGMA foreign_keys = ON;` enforces referential integrity across runs, steps, lineage, and invocations.

## Consequences
- **Positive:** Zero external setup; embedded in Python standard library; portable `.db` file for easy inspection, debugging, and backup.
- **Negative:** Concurrency is limited to a single writer process at a time.
- **Reversal Trigger:** If multi-node distributed concurrency or remote client connections become required.

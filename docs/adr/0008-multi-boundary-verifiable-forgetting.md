# ADR-0008: Multi-Boundary Verifiable Forgetting and Retention Semantics

## Status
Accepted

## Context
Privacy regulations (e.g., GDPR Article 17 "Right to be Forgotten") and memory hygiene require that agents reliably forget information. Many systems merely delete a vector from an external index while leaving traces in transcripts, checkpoints, cache stores, and exported logs. Moreover, naive deletion without tombstones destroys lineage history and auditability.

## Decision
We implement a **Multi-Boundary Verifiable Forgetting Architecture**:
1. Differentiate **Retrieval Exclusion** (logical hiding via `state = 'deleted'`, TTL expiration, and tombstone queries) from **Physical Erasure** (`DELETE`, SQLite `VACUUM`, and secure file shredding).
2. Retention and deletion policies are explicitly coordinated across six boundaries:
   - Memory records (`memory_records`)
   - Trace events (`trace_events`)
   - Checkpoints (`checkpoints`)
   - In-memory caches and scratchpads
   - Exported artifacts (JSONL, benchmark reports)
   - Backups and restores
3. Deletion generates a durable `TombstoneRecord` containing `target_memory_id`, `namespace_id`, timestamp, and deletion reason, while scrubbing the memory payload.
4. SQL queries enforce zero-retrieval invariants via composite index lookups.

## Consequences
- **Positive:** Provable 0% retrieval rate of deleted/expired items; GDPR audit compliance; clean separation of logical filtering and physical destruction.
- **Negative:** Tombstones accumulate over time and require separate retention pruning policies.
- **Reversal Trigger:** None; core privacy and compliance requirement.

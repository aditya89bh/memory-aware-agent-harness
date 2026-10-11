# ADR-0008: Multi-Boundary Verifiable Forgetting and Retention Semantics

## Status
Accepted

## Context
Privacy regulations (e.g., GDPR Article 17 "Right to be Forgotten") and memory hygiene require that agents reliably forget information. Many systems merely delete a vector from an external index while leaving traces in transcripts, checkpoints, cache stores, derived knowledge, and exported logs. Moreover, naive deletion without tombstones destroys lineage history and auditability. Additionally, claiming guaranteed physical data destruction on modern flash storage ignores hardware flash translation layer (FTL) wear-leveling and uncheckpointed WAL frames.

## Decision
We implement a **Multi-Boundary Verifiable Forgetting Architecture**:
1. Differentiate **Retrieval Exclusion** (logical hiding via `state = 'deleted'`, TTL expiration, and tombstone queries) from **Software Deletion & WAL Compaction** (`DELETE`, SQLite `VACUUM`, and `wal_checkpoint(TRUNCATE)`), while explicitly acknowledging hardware wear-leveling physical realities.
2. Retention and deletion policies are explicitly coordinated across six boundaries:
   - Memory records (`memory_records`): Software row deletion and WAL frame truncation.
   - Derived memories (consolidation products): Automatic lineage invalidation; entries derived from purged records are flagged as `origin_status = 'ORIGIN_PURGED'` and scheduled for re-consolidation or eviction.
   - Trace events (`trace_events`): Hard deletion of event payloads older than `retention_days`; sequence counters preserved.
   - Checkpoints (`checkpoints`): Cascading purge of snapshots referencing deleted runs.
   - In-memory caches and scratchpads: Instant key invalidation on step boundary or tombstone event.
   - Exported artifacts and backups: Filter pipelines exclude tombstoned data from JSONL exports and backups.
3. Deletion generates a durable `TombstoneRecord` containing `target_memory_id`, `namespace_id`, timestamp, and deletion reason, while scrubbing the memory payload.
4. SQL queries enforce zero-retrieval invariants via composite index lookups.

## Consequences
- **Positive:** Provable 0% retrieval rate of deleted/expired items; GDPR audit compliance; automatic invalidation of derived knowledge; realistic security posture.
- **Negative:** Tombstones accumulate over time and require separate retention pruning policies.
- **Reversal Trigger:** None; core privacy and compliance requirement.

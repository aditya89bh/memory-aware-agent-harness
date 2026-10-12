# ADR-0008: Multi-Boundary Verifiable Forgetting and Retention Semantics

## Status
Accepted

## Context
Privacy regulations and memory hygiene require that agents reliably forget information and support user data erasure requests. Many systems merely delete an entry from an external index while leaving traces in transcripts, checkpoints, cache stores, derived knowledge, and exported logs. Moreover, naive deletion without tombstones destroys lineage history and auditability. Additionally, claiming guaranteed physical data destruction on modern flash storage ignores hardware flash translation layer (FTL) wear-leveling and uncheckpointed WAL frames.

## Decision
We implement a **Multi-Boundary Verifiable Forgetting Architecture**:
1. Differentiate **Retrieval Exclusion** (logical hiding via `state = 'deleted'`, TTL expiration, and tombstone filtering in queries) from **Software Deletion & WAL Compaction** (`PRAGMA secure_delete = ON; DELETE`, SQLite `VACUUM`, and `wal_checkpoint(TRUNCATE)`), while explicitly acknowledging hardware wear-leveling physical realities. SQLite user-space operations zero database pages, but cannot guarantee physical destruction of SSD NAND flash blocks managed by controller Flash Translation Layers (FTL).
2. Retention and deletion policies are explicitly coordinated across six boundaries:
   - Memory records (`memory_records`): Software row deletion with `PRAGMA secure_delete = ON` and WAL frame truncation via `wal_checkpoint(TRUNCATE)`.
   - Derived memories (consolidation products): Automatic lineage invalidation; entries derived from purged records are flagged as `origin_status = 'ORIGIN_PURGED'` and scheduled for re-consolidation or eviction.
   - Trace events (`trace_events`): Hard deletion or redaction of event payloads older than configured `retention_days`; sequence counters and tombstone audit records preserved. Erased memory content embedded in historical trace logs is scrubbed during retention pruning.
   - Checkpoints (`checkpoints`): Cascading purge of snapshots referencing deleted runs.
   - In-memory caches and scratchpads: Instant key invalidation on step boundary or tombstone event.
   - Exported artifacts and backups: Filter pipelines exclude tombstoned data from JSONL exports and backups.
3. **Checkpoint Restoration Safeguard (Anti-Resurrection):** Checkpoint snapshots contain serialized state payloads that may embed memory content captured before erasure. To prevent restoring a historical checkpoint from resurrecting erased memories, checkpoint rehydration must perform **Tombstone Reconciliation**: the rehydration engine queries the `tombstones` table for the target namespace and strips all tombstoned memory IDs from the restored working memory and candidate pool before resuming execution.
4. Deletion generates a durable `TombstoneRecord` containing `target_memory_id`, `namespace_id`, timestamp, and deletion reason, while scrubbing the memory payload.
5. SQL queries enforce retrieval exclusion via parameterized composite index lookups (`WHERE state = 'active'`).

## Consequences
- **Positive:** Enforced software-level exclusion of deleted/expired items from active retrieval queries (`WHERE state = 'active'`); durable audit trail of erasure requests via tombstones; automatic lineage invalidation; checkpoint restoration safeguards prevent resurrecting erased data; realistic security posture without unsupported physical destruction or blanket regulatory compliance guarantees.
- **Negative:** Tombstones accumulate over time and require separate retention pruning policies; historical trace events require retention-based payload compaction.
- **Reversal Trigger:** None; core privacy and data governance requirement.

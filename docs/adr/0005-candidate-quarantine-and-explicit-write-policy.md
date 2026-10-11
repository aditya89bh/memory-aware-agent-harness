# ADR-0005: Memory Candidate Quarantine and Policy-Gated Ingestion

## Status
Accepted

## Context
Many agent implementations write raw conversation transcripts or unvalidated tool observations directly into persistent memory. This leads to memory poisoning, rapid bloat, storage of sensitive credentials, and hallucination loops where errors in early steps are treated as ground-truth facts in later sessions.

## Decision
We enforce an explicit **Candidate Quarantine and Write Policy Lifecycle**:
1. Post-step observation processing creates transient `MemoryCandidate` objects rather than permanent memory records.
2. A configurable `WritePolicy` validates candidates against strict allowlists, deduplication rules, schema checks, and confidence thresholds.
3. Only approved candidates transition to durable `MemoryRecord` entries in SQLite.
4. Rejected candidates are recorded in telemetry with structured rejection reason codes (`REJECT_DUPLICATE`, `REJECT_LOW_CONFIDENCE`, `REJECT_SCHEMA_VIOLATION`, `REJECT_UNTRUSTED_SOURCE`).

## Consequences
- **Positive:** Protects long-term memory from poisoning and junk accumulation; provides an audit trail of rejected memory proposals.
- **Negative:** Extra processing stage per step; strict policies may reject nuanced implicit observations unless policies are tuned.
- **Reversal Trigger:** None; this is an invariant safety requirement.

"""Database schema definitions, table DDL, and indexes for SQLite WAL persistence."""

from __future__ import annotations

SCHEMA_V1_DDL: str = """
-- 1. Runs Table
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    namespace_id TEXT NOT NULL,
    status TEXT NOT NULL,
    step_count INTEGER NOT NULL DEFAULT 0,
    max_steps INTEGER NOT NULL,
    token_budget INTEGER NOT NULL,
    tokens_consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_namespace ON runs(namespace_id, created_at);

-- 2. Run Steps Table
CREATE TABLE IF NOT EXISTS run_steps (
    step_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    namespace_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    state TEXT NOT NULL,
    context_manifest_id TEXT,
    model_decision_json TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    step_result_json TEXT,
    UNIQUE(run_id, step_number)
);
CREATE INDEX IF NOT EXISTS idx_run_steps_namespace_run
ON run_steps(namespace_id, run_id, step_number);

-- 3. Unified Memory Records Table (All 4 Categories)
CREATE TABLE IF NOT EXISTS memory_records (
    memory_id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    state TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    salience REAL NOT NULL DEFAULT 0.5,
    origin TEXT NOT NULL DEFAULT 'external_retrieval',
    source_references_json TEXT,
    access_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    run_id TEXT,
    branch_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_active
ON memory_records(namespace_id, kind, state)
WHERE state = 'active';
CREATE INDEX IF NOT EXISTS idx_memories_lookup ON memory_records(namespace_id, memory_id);

-- 4. Memory Lineage & Provenance Table
CREATE TABLE IF NOT EXISTS memory_lineage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace_id TEXT NOT NULL,
    target_memory_id TEXT NOT NULL REFERENCES memory_records(memory_id) ON DELETE CASCADE,
    source_reference_id TEXT NOT NULL,
    relationship TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_target ON memory_lineage(namespace_id, target_memory_id);

-- 5. Tombstones Table (Physical Audit of Verifiable Deletion)
CREATE TABLE IF NOT EXISTS tombstones (
    tombstone_id TEXT PRIMARY KEY,
    target_memory_id TEXT NOT NULL,
    namespace_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    tombstoned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tombstones_lookup ON tombstones(namespace_id, target_memory_id);

-- 6. Tool Invocations Table
CREATE TABLE IF NOT EXISTS tool_invocations (
    invocation_id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL REFERENCES run_steps(step_id) ON DELETE CASCADE,
    namespace_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    side_effect TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error_text TEXT,
    dispatched_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(namespace_id, tool_name, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_tool_idempotency
ON tool_invocations(namespace_id, tool_name, idempotency_key);

-- 7. Checkpoints Table
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    step_id TEXT NOT NULL REFERENCES run_steps(step_id) ON DELETE CASCADE,
    branch_id TEXT NOT NULL,
    namespace_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    state_payload_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_lookup ON checkpoints(namespace_id, run_id, branch_id);

-- 8. Trace Events Table (Append-Only Log)
CREATE TABLE IF NOT EXISTS trace_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    step_id TEXT,
    namespace_id TEXT NOT NULL,
    sequence_num INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    emitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_sequence ON trace_events(run_id, sequence_num);
CREATE INDEX IF NOT EXISTS idx_events_namespace ON trace_events(namespace_id, run_id, sequence_num);
"""

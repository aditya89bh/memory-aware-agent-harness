"""Independent audit verification for Phase 2 Medium severity defects.

Validates:
1. Version regression rejection in put_memory (StorageVersionConflictError).
2. Content and metadata scrubbing on tombstone_memory.
3. as_of TTL expiration filtering in adapter.search and adapter.list_active.
4. Tokenized multi-term search and pre-limit relevance scoring.
5. Wildcard query sanitization (% and _).
6. OperationalError wrapped in typed StorageError with recoverable=True.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from memory_aware_agent_harness.contracts import (
    FrozenDict,
    MemoryFilter,
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    NamespaceId,
    StorageError,
    StorageVersionConflictError,
)
from memory_aware_agent_harness.storage.adapter import SQLiteStorageAdapter
from memory_aware_agent_harness.storage.memory_repo import (
    get_memory,
    search_memories,
)


def _make_record(
    memory_id: str,
    namespace_id: str = "ns_audit",
    *,
    version: int = 1,
    content: str = "Sample content",
    salience: float = 0.5,
    confidence: float = 0.9,
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
    metadata: dict[str, object] | None = None,
) -> MemoryRecord:
    now = created_at or datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    return MemoryRecord(
        id=MemoryId(memory_id),
        namespace_id=NamespaceId(namespace_id),
        kind=MemoryKind.SEMANTIC,
        state=MemoryState.ACTIVE,
        content=content,
        salience=salience,
        confidence=confidence,
        version=version,
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
        origin=MemoryOrigin.EXTERNAL_RETRIEVAL,
        metadata=FrozenDict(metadata or {}),
    )


def test_put_memory_rejects_version_regression(tmp_path: Path) -> None:
    """put_memory must refuse version regressions when incoming version < stored version."""
    db_path = tmp_path / "version_regression.db"
    adapter = SQLiteStorageAdapter(db_path)
    try:
        ns = NamespaceId("ns_audit")
        rec_v5 = _make_record("mem_user_pref", version=5, content="Prefers Earl Grey")
        adapter.put(rec_v5)

        stored = adapter.get(rec_v5.id, ns)
        assert stored is not None
        assert stored.version == 5
        assert stored.content == "Prefers Earl Grey"

        # Attempting to overwrite with version 2 must be rejected
        rec_v2 = _make_record("mem_user_pref", version=2, content="Prefers Green Tea")
        with pytest.raises(StorageVersionConflictError) as exc_info:
            adapter.put(rec_v2)

        assert exc_info.value.expected_version == 5
        assert exc_info.value.actual_version == 2
        assert "Version regression rejected" in str(exc_info.value)

        # Stored record remains unmodified
        stored_after = adapter.get(rec_v5.id, ns)
        assert stored_after is not None
        assert stored_after.version == 5
        assert stored_after.content == "Prefers Earl Grey"

        # Overwrite with same version (idempotent write) or higher version succeeds
        rec_v6 = _make_record("mem_user_pref", version=6, content="Prefers Matcha")
        adapter.put(rec_v6)
        stored_v6 = adapter.get(rec_v5.id, ns)
        assert stored_v6 is not None
        assert stored_v6.version == 6
        assert stored_v6.content == "Prefers Matcha"
    finally:
        adapter.close()


def test_tombstone_scrubs_content_and_metadata_in_storage(tmp_path: Path) -> None:
    """Tombstoning must scrub raw sensitive content and metadata from the database row."""
    db_path = tmp_path / "tombstone_scrub.db"
    adapter = SQLiteStorageAdapter(db_path)
    try:
        ns = NamespaceId("ns_audit")
        rec = _make_record(
            "mem_secret",
            content="Sensitive credit card: 4111-2222-3333-4444",
            metadata={"confidential": True, "cardholder": "Alice"},
        )
        adapter.put(rec)

        # Raw verification before tombstone
        raw_before = adapter._conn.execute(
            "SELECT content, metadata_json, state FROM memory_records WHERE memory_id = ?;",
            (str(rec.id),),
        ).fetchone()
        assert "4111" in raw_before["content"]
        assert raw_before["state"] == "active"
        assert raw_before["metadata_json"] is not None

        # Tombstone the record
        adapter.tombstone(rec.id, ns, reason="GDPR erasure request")

        # Raw DB check: content and metadata must be scrubbed!
        raw_after = adapter._conn.execute(
            "SELECT content, metadata_json, state FROM memory_records WHERE memory_id = ?;",
            (str(rec.id),),
        ).fetchone()
        assert raw_after["content"] == "[DELETED]"
        assert "4111" not in raw_after["content"]
        assert raw_after["metadata_json"] is None
        assert raw_after["state"] == "deleted"

        # Standard get returns None
        assert adapter.get(rec.id, ns) is None

        # get_memory with include_tombstoned preserves lineage audit without sensitive content
        tombstoned_rec = get_memory(adapter._conn, rec.id, ns, include_tombstoned=True)
        assert tombstoned_rec is not None
        assert tombstoned_rec.state == MemoryState.DELETED
        assert tombstoned_rec.content == "[DELETED]"
    finally:
        adapter.close()


def test_search_and_list_active_honors_as_of_ttl(tmp_path: Path) -> None:
    """adapter.search and adapter.list_active must respect the injected as_of timestamp."""
    db_path = tmp_path / "ttl_filtering.db"
    adapter = SQLiteStorageAdapter(db_path)
    try:
        ns = NamespaceId("ns_audit")
        ref_time = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)

        # Expired memory
        rec_expired = _make_record(
            "m_exp",
            content="Temporary verification code: 123456",
            expires_at=ref_time - timedelta(minutes=5),
            created_at=ref_time - timedelta(hours=1),
        )
        # Active memory
        rec_active = _make_record(
            "m_valid",
            content="Permanent user setting: dark mode",
            expires_at=ref_time + timedelta(days=7),
            created_at=ref_time - timedelta(hours=1),
        )
        adapter.put(rec_expired)
        adapter.put(rec_active)

        # 1. list_active with as_of excludes expired record
        active_items = adapter.list_active(ns, MemoryKind.SEMANTIC, as_of=ref_time)
        active_ids = {m.id for m in active_items}
        assert MemoryId("m_valid") in active_ids
        assert MemoryId("m_exp") not in active_ids

        # 2. search with as_of excludes expired record
        search_results = adapter.search(
            ns,
            query="code setting",
            kinds=[MemoryKind.SEMANTIC],
            limit=10,
            as_of=ref_time,
        )
        search_ids = {m.id for m in search_results}
        assert MemoryId("m_valid") in search_ids
        assert MemoryId("m_exp") not in search_ids
    finally:
        adapter.close()


def test_search_tokenization_and_pre_limit_relevance_scoring(tmp_path: Path) -> None:
    """Multi-term queries tokenize and score before limiting, preventing high-salience masking."""
    db_path = tmp_path / "search_tokenization.db"
    adapter = SQLiteStorageAdapter(db_path)
    try:
        ns = NamespaceId("ns_audit")
        now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)

        # Target memory: highly relevant keywords, but lower salience
        target_rec = _make_record(
            "mem_pref",
            content="The user prefers green tea with honey and lemon in the morning.",
            salience=0.3,
            confidence=0.9,
            created_at=now,
        )
        # Distractor memories: irrelevant keywords, high salience
        for i in range(10):
            distractor = _make_record(
                f"mem_noise_{i}",
                content=f"Generic background facts about system infrastructure component {i}.",
                salience=0.95,
                confidence=0.9,
                created_at=now,
            )
            adapter.put(distractor)

        adapter.put(target_rec)

        # Query uses terms not matching full exact substring: "What drink does the user prefer?"
        query_filter = MemoryFilter(
            namespace_id=ns,
            query_text="What drink does the user prefer?",
            limit=3,
            as_of=now,
        )
        scored = adapter.search_scored(query_filter)

        # The target memory must be retrieved and ranked #1 despite lower salience
        assert len(scored) > 0
        top_hit = scored[0]
        assert top_hit.record.id == MemoryId("mem_pref")
        assert top_hit.score_breakdown["relevance"] > 0.0
    finally:
        adapter.close()


def test_search_wildcard_sanitization(tmp_path: Path) -> None:
    """Wildcard-only queries like % must not dump all records."""
    db_path = tmp_path / "wildcard_sanitization.db"
    adapter = SQLiteStorageAdapter(db_path)
    try:
        ns = NamespaceId("ns_audit")
        for i in range(5):
            adapter.put(_make_record(f"mem_{i}", content=f"Fact number {i}"))

        # Query of '%' or '_' should return empty, not all 5 records
        results = search_memories(
            adapter._conn,
            MemoryFilter(namespace_id=ns, query_text="%", limit=10),
        )
        assert len(results) == 0

        results_underscore = search_memories(
            adapter._conn,
            MemoryFilter(namespace_id=ns, query_text="___", limit=10),
        )
        assert len(results_underscore) == 0
    finally:
        adapter.close()


def test_adapter_wraps_operational_error_in_storage_error() -> None:
    """SQLite OperationalErrors (e.g. database is locked) are wrapped in typed StorageError."""
    mock_conn = MagicMock(spec=sqlite3.Connection)
    mock_conn.execute.side_effect = sqlite3.OperationalError("database is locked")

    adapter = SQLiteStorageAdapter(connection=mock_conn, auto_migrate=False)

    rec = _make_record("mem_lock_test")
    with pytest.raises(StorageError) as exc_info:
        adapter.put(rec)

    assert exc_info.value.recoverable is True
    assert "Database operational error in put" in str(exc_info.value)

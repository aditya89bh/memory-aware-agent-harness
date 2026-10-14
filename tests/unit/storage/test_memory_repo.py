"""Unit tests for SQLite memory records repository with optimistic concurrency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memory_aware_agent_harness.contracts import (
    BranchId,
    CrossNamespaceAccessError,
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    StorageVersionConflictError,
    TombstoneActiveError,
)
from memory_aware_agent_harness.storage.connection import create_connection
from memory_aware_agent_harness.storage.memory_repo import (
    get_memory,
    get_memory_lineage,
    is_tombstoned,
    list_active_memories,
    put_memory,
    tombstone_memory,
    update_memory_optimistic,
)
from memory_aware_agent_harness.storage.migrations import apply_migrations


def _make_sample_memory(
    memory_id: str = "mem_01",
    namespace_id: str = "ns_alpha",
    kind: MemoryKind = MemoryKind.EPISODIC,
    content: str = "User preferences for notifications",
    state: MemoryState = MemoryState.ACTIVE,
    version: int = 1,
    origin: MemoryOrigin = MemoryOrigin.AGENT_OBSERVATION,
    expires_at: datetime | None = None,
    source_references: tuple[str, ...] = ("ref_01", "ref_02"),
) -> MemoryRecord:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    return MemoryRecord(
        id=MemoryId(memory_id),
        namespace_id=NamespaceId(namespace_id),
        kind=kind,
        content=content,
        state=state,
        confidence=0.95,
        salience=0.8,
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
        source_references=source_references,
        metadata={"tags": ["preferences", "alerts"], "flag": True},
        access_count=3,
        version=version,
        origin=origin,
        run_id=RunId("run_100"),
        branch_id=BranchId("main"),
    )


def test_put_and_get_memory_roundtrip() -> None:
    """put_memory stores full record and get_memory reconstructs all fields accurately."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        mem = _make_sample_memory()
        put_memory(conn, mem)

        fetched = get_memory(conn, mem.id, mem.namespace_id)
        assert fetched is not None
        assert fetched.id == mem.id
        assert fetched.namespace_id == mem.namespace_id
        assert fetched.kind == mem.kind
        assert fetched.content == mem.content
        assert fetched.state == mem.state
        assert fetched.confidence == mem.confidence
        assert fetched.salience == mem.salience
        assert fetched.origin == mem.origin
        assert fetched.version == mem.version
        assert fetched.access_count == mem.access_count
        assert fetched.source_references == ("ref_01", "ref_02")
        assert fetched.metadata == {"tags": ("preferences", "alerts"), "flag": True}
        assert fetched.run_id == RunId("run_100")
        assert fetched.branch_id == BranchId("main")

        # Lineage edges are recorded
        lineage = get_memory_lineage(conn, mem.id, mem.namespace_id)
        assert lineage == ("ref_01", "ref_02")

        # Nonexistent memory returns None
        assert get_memory(conn, MemoryId("missing"), mem.namespace_id) is None
    finally:
        conn.close()


def test_update_memory_optimistic_concurrency() -> None:
    """update_memory_optimistic succeeds on matching version and fails on conflict."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        mem = _make_sample_memory(version=1)
        put_memory(conn, mem)

        # 1. Update with matching expected_version=1 succeeds and increments version to 2
        now2 = datetime(2026, 10, 14, 12, 10, 0, tzinfo=UTC)
        updated_mem = MemoryRecord(
            id=mem.id,
            namespace_id=mem.namespace_id,
            kind=mem.kind,
            content="Updated preference text",
            state=mem.state,
            confidence=0.99,
            salience=0.85,
            created_at=mem.created_at,
            updated_at=now2,
            version=1,  # will be incremented
        )
        update_memory_optimistic(conn, updated_mem, expected_version=1)

        fetched = get_memory(conn, mem.id, mem.namespace_id)
        assert fetched is not None
        assert fetched.version == 2
        assert fetched.content == "Updated preference text"
        assert fetched.confidence == 0.99

        # 2. Update with stale expected_version=1 raises StorageVersionConflictError
        with pytest.raises(StorageVersionConflictError) as exc_info:
            update_memory_optimistic(conn, updated_mem, expected_version=1)
        assert exc_info.value.expected_version == 1
        assert exc_info.value.actual_version == 2
        assert exc_info.value.record_id == str(mem.id)

        # 3. Nonexistent record raises RecordNotFoundError
        missing = _make_sample_memory(memory_id="missing_mem")
        with pytest.raises(RecordNotFoundError):
            update_memory_optimistic(conn, missing, expected_version=1)
    finally:
        conn.close()


def test_tombstone_lifecycle_and_lineage_preservation() -> None:
    """Tombstoning hides memory, prevents mutations, but preserves audit and lineage."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        mem = _make_sample_memory(source_references=("source_doc_42",))
        put_memory(conn, mem)

        # Initially active and not tombstoned
        assert not is_tombstoned(conn, mem.id, mem.namespace_id)

        # Tombstone the memory
        tomb_id = tombstone_memory(
            conn,
            mem.id,
            mem.namespace_id,
            reason="User GDPR right-to-be-forgotten request",
        )
        assert tomb_id == f"tomb_{mem.id}"
        assert is_tombstoned(conn, mem.id, mem.namespace_id)

        # get_memory without include_tombstoned returns None
        assert get_memory(conn, mem.id, mem.namespace_id) is None

        # get_memory with include_tombstoned=True returns deleted record
        tomb_rec = get_memory(conn, mem.id, mem.namespace_id, include_tombstoned=True)
        assert tomb_rec is not None
        assert tomb_rec.state == MemoryState.DELETED

        # CRITICAL: Lineage must be preserved!
        lineage = get_memory_lineage(conn, mem.id, mem.namespace_id)
        assert lineage == ("source_doc_42",)

        # Re-tombstoning raises TombstoneActiveError
        with pytest.raises(TombstoneActiveError):
            tombstone_memory(conn, mem.id, mem.namespace_id, reason="Duplicate delete")

        # Mutating via put_memory raises TombstoneActiveError
        with pytest.raises(TombstoneActiveError):
            put_memory(conn, mem)

        # Mutating via update_memory_optimistic raises TombstoneActiveError
        with pytest.raises(TombstoneActiveError):
            update_memory_optimistic(conn, mem, expected_version=1)

        # Tombstoning nonexistent memory raises RecordNotFoundError
        with pytest.raises(RecordNotFoundError):
            tombstone_memory(conn, MemoryId("missing"), mem.namespace_id, reason="test")

        # Active record with out-of-band tombstone is hidden by get_memory
        mem_oob = _make_sample_memory(memory_id="mem_oob")
        put_memory(conn, mem_oob)
        conn.execute(
            """
            INSERT INTO tombstones (
                tombstone_id, target_memory_id, namespace_id, reason, tombstoned_at
            ) VALUES ('t_oob', 'mem_oob', 'ns_alpha', 'oob reason', '2026-10-14T12:00:00+00:00');
            """
        )
        assert get_memory(conn, mem_oob.id, mem_oob.namespace_id) is None
    finally:
        conn.close()


def test_list_active_memories_filtering_and_ttl() -> None:
    """list_active_memories filters by kind, state, and as_of expiration."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        ns = NamespaceId("ns_alpha")
        now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

        # Active memory with no expiration
        mem_active = _make_sample_memory(
            memory_id="m_active",
            kind=MemoryKind.SEMANTIC,
            content="Persistent fact",
        )
        put_memory(conn, mem_active)

        # Active memory with future expiration
        mem_future = _make_sample_memory(
            memory_id="m_future",
            kind=MemoryKind.SEMANTIC,
            content="Expires tomorrow",
            expires_at=now + timedelta(days=1),
        )
        put_memory(conn, mem_future)

        # Active memory with past expiration (expired)
        mem_expired = _make_sample_memory(
            memory_id="m_expired",
            kind=MemoryKind.SEMANTIC,
            content="Expired yesterday",
            expires_at=now - timedelta(days=1),
        )
        put_memory(conn, mem_expired)

        # Memory with different kind (EPISODIC)
        mem_episodic = _make_sample_memory(
            memory_id="m_episodic",
            kind=MemoryKind.EPISODIC,
            content="Different kind",
        )
        put_memory(conn, mem_episodic)

        # Tombstoned memory of same kind
        mem_tombstoned = _make_sample_memory(
            memory_id="m_tomb",
            kind=MemoryKind.SEMANTIC,
            content="Tombstoned fact",
        )
        put_memory(conn, mem_tombstoned)
        tombstone_memory(conn, mem_tombstoned.id, ns, reason="Superseded")

        # 1. list_active_memories without as_of returns non-tombstoned semantic memories
        active_no_ttl = list_active_memories(conn, ns, MemoryKind.SEMANTIC)
        ids_no_ttl = [m.id for m in active_no_ttl]
        assert MemoryId("m_active") in ids_no_ttl
        assert MemoryId("m_future") in ids_no_ttl
        assert MemoryId("m_expired") in ids_no_ttl
        assert MemoryId("m_tomb") not in ids_no_ttl
        assert MemoryId("m_episodic") not in ids_no_ttl

        # 2. list_active_memories with as_of=now filters out expired memory
        active_with_ttl = list_active_memories(conn, ns, MemoryKind.SEMANTIC, as_of=now)
        ids_with_ttl = [m.id for m in active_with_ttl]
        assert ids_with_ttl == [MemoryId("m_active"), MemoryId("m_future")]
        assert MemoryId("m_expired") not in ids_with_ttl
    finally:
        conn.close()


def test_memory_cross_namespace_access_denied() -> None:
    """All memory operations strictly enforce tenant namespace scoping."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        mem_a = _make_sample_memory(memory_id="mem_a", namespace_id="ns_alpha")
        put_memory(conn, mem_a)

        # 1. Cross-namespace read
        with pytest.raises(CrossNamespaceAccessError):
            get_memory(conn, mem_a.id, NamespaceId("ns_beta"))

        # 2. Cross-namespace put attempt with existing memory_id in another namespace
        tampered_put = _make_sample_memory(memory_id="mem_a", namespace_id="ns_beta")
        with pytest.raises(CrossNamespaceAccessError):
            put_memory(conn, tampered_put)

        # 3. Cross-namespace optimistic update
        with pytest.raises(CrossNamespaceAccessError):
            update_memory_optimistic(conn, tampered_put, expected_version=1)

        # 4. Cross-namespace tombstone
        with pytest.raises(CrossNamespaceAccessError):
            tombstone_memory(conn, mem_a.id, NamespaceId("ns_beta"), reason="Unauthorized")

        # 5. List active under empty namespace returns empty tuple
        assert list_active_memories(conn, NamespaceId("ns_beta"), MemoryKind.EPISODIC) == ()
    finally:
        conn.close()

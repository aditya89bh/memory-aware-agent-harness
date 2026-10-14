"""Unit tests for scoped multi-dimensional memory search filtering."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from memory_aware_agent_harness.contracts import (
    BranchId,
    MemoryFilter,
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    NamespaceId,
    RunId,
)
from memory_aware_agent_harness.storage.connection import create_connection
from memory_aware_agent_harness.storage.memory_repo import (
    put_memory,
    search_memories,
    search_memories_basic,
    tombstone_memory,
)
from memory_aware_agent_harness.storage.migrations import apply_migrations


def _populate_test_memories(conn: sqlite3.Connection) -> None:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    records = [
        MemoryRecord(
            id=MemoryId("mem_alpha_01"),
            namespace_id=NamespaceId("ns_alpha"),
            kind=MemoryKind.SEMANTIC,
            content="Python 3.14 supports deferred evaluation of annotations",
            state=MemoryState.ACTIVE,
            confidence=0.95,
            salience=0.9,
            created_at=now,
            updated_at=now,
            origin=MemoryOrigin.AGENT_OBSERVATION,
            run_id=RunId("run_1"),
            branch_id=BranchId("main"),
        ),
        MemoryRecord(
            id=MemoryId("mem_alpha_02"),
            namespace_id=NamespaceId("ns_alpha"),
            kind=MemoryKind.EPISODIC,
            content="User requested fast turnaround on database migrations",
            state=MemoryState.ACTIVE,
            confidence=0.85,
            salience=0.7,
            created_at=now + timedelta(minutes=1),
            updated_at=now + timedelta(minutes=1),
            origin=MemoryOrigin.USER_EXPLICIT,
            run_id=RunId("run_1"),
            branch_id=BranchId("feature_db"),
        ),
        MemoryRecord(
            id=MemoryId("mem_alpha_03"),
            namespace_id=NamespaceId("ns_alpha"),
            kind=MemoryKind.PROCEDURAL,
            content="How to run tests with ruff and strict mypy",
            state=MemoryState.ACTIVE,
            confidence=0.99,
            salience=0.8,
            created_at=now + timedelta(minutes=2),
            updated_at=now + timedelta(minutes=2),
            origin=MemoryOrigin.SYSTEM_PROVIDED,
            run_id=RunId("run_2"),
            branch_id=BranchId("main"),
        ),
        MemoryRecord(
            id=MemoryId("mem_alpha_expired"),
            namespace_id=NamespaceId("ns_alpha"),
            kind=MemoryKind.SEMANTIC,
            content="Short-lived cache entry about Python runtime",
            state=MemoryState.ACTIVE,
            confidence=0.9,
            salience=0.5,
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
            expires_at=now - timedelta(minutes=10),
            origin=MemoryOrigin.AGENT_OBSERVATION,
            run_id=RunId("run_1"),
            branch_id=BranchId("main"),
        ),
        MemoryRecord(
            id=MemoryId("mem_alpha_tomb"),
            namespace_id=NamespaceId("ns_alpha"),
            kind=MemoryKind.SEMANTIC,
            content="Outdated Python fact to be tombstoned",
            state=MemoryState.ACTIVE,
            confidence=0.9,
            salience=0.5,
            created_at=now,
            updated_at=now,
            origin=MemoryOrigin.AGENT_OBSERVATION,
            run_id=RunId("run_1"),
            branch_id=BranchId("main"),
        ),
        MemoryRecord(
            id=MemoryId("mem_beta_01"),
            namespace_id=NamespaceId("ns_beta"),
            kind=MemoryKind.SEMANTIC,
            content="Tenant beta Python document",
            state=MemoryState.ACTIVE,
            confidence=0.95,
            salience=0.9,
            created_at=now,
            updated_at=now,
            origin=MemoryOrigin.AGENT_OBSERVATION,
            run_id=RunId("run_beta_1"),
            branch_id=BranchId("main"),
        ),
    ]

    for rec in records:
        put_memory(conn, rec)

    # Tombstone the targeted record
    tombstone_memory(
        conn,
        MemoryId("mem_alpha_tomb"),
        NamespaceId("ns_alpha"),
        reason="Fact invalidated",
    )


def test_search_memories_namespace_isolation() -> None:
    """search_memories strictly scopes results to the requested namespace."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        _populate_test_memories(conn)

        # Search for 'Python' in ns_alpha
        results_alpha = search_memories(
            conn,
            MemoryFilter(
                namespace_id=NamespaceId("ns_alpha"),
                query_text="Python",
            ),
        )
        assert len(results_alpha) >= 1
        assert all(m.namespace_id == NamespaceId("ns_alpha") for m in results_alpha)
        assert not any(m.id == MemoryId("mem_beta_01") for m in results_alpha)

        # Search for 'Python' in ns_beta
        results_beta = search_memories(
            conn,
            MemoryFilter(
                namespace_id=NamespaceId("ns_beta"),
                query_text="Python",
            ),
        )
        assert len(results_beta) == 1
        assert results_beta[0].id == MemoryId("mem_beta_01")
    finally:
        conn.close()


def test_search_memories_kind_and_text_filter() -> None:
    """search_memories filters by kind category and content text query."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        _populate_test_memories(conn)
        ns = NamespaceId("ns_alpha")

        # Query procedural memories
        proc_results = search_memories(
            conn,
            MemoryFilter(
                namespace_id=ns,
                kinds=(MemoryKind.PROCEDURAL,),
            ),
        )
        assert len(proc_results) == 1
        assert proc_results[0].id == MemoryId("mem_alpha_03")

        # Text query matching episodic
        text_results = search_memories(
            conn,
            MemoryFilter(
                namespace_id=ns,
                query_text="migrations",
            ),
        )
        assert len(text_results) == 1
        assert text_results[0].id == MemoryId("mem_alpha_02")

        # Basic search matching MemoryStoreProtocol
        basic_results = search_memories_basic(
            conn,
            namespace_id=ns,
            query="Python",
            kinds=[MemoryKind.SEMANTIC],
        )
        assert len(basic_results) >= 1
        assert all(m.kind == MemoryKind.SEMANTIC for m in basic_results)
    finally:
        conn.close()


def test_search_memories_run_branch_and_origin_filters() -> None:
    """search_memories filters by run_id, branch_id, and origin provenance."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        _populate_test_memories(conn)
        ns = NamespaceId("ns_alpha")

        # Filter by run_id
        run2_results = search_memories(
            conn,
            MemoryFilter(
                namespace_id=ns,
                run_id=RunId("run_2"),
            ),
        )
        assert len(run2_results) == 1
        assert run2_results[0].id == MemoryId("mem_alpha_03")

        # Filter by branch_id
        feature_results = search_memories(
            conn,
            MemoryFilter(
                namespace_id=ns,
                branch_id=BranchId("feature_db"),
            ),
        )
        assert len(feature_results) == 1
        assert feature_results[0].id == MemoryId("mem_alpha_02")

        # Filter by origin
        user_results = search_memories(
            conn,
            MemoryFilter(
                namespace_id=ns,
                origin=MemoryOrigin.USER_EXPLICIT,
            ),
        )
        assert len(user_results) == 1
        assert user_results[0].id == MemoryId("mem_alpha_02")
    finally:
        conn.close()


def test_search_memories_confidence_salience_and_ttl() -> None:
    """search_memories enforces confidence and salience floors and as_of TTL expiration."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        _populate_test_memories(conn)
        ns = NamespaceId("ns_alpha")
        now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

        # High confidence floor
        high_conf = search_memories(
            conn,
            MemoryFilter(
                namespace_id=ns,
                min_confidence=0.96,
            ),
        )
        assert len(high_conf) == 1
        assert high_conf[0].id == MemoryId("mem_alpha_03")

        # High salience floor
        high_sal = search_memories(
            conn,
            MemoryFilter(
                namespace_id=ns,
                min_salience=0.85,
            ),
        )
        assert len(high_sal) == 1
        assert high_sal[0].id == MemoryId("mem_alpha_01")

        # as_of TTL filter excludes expired record
        unexpired = search_memories(
            conn,
            MemoryFilter(
                namespace_id=ns,
                as_of=now,
                query_text="Python",
            ),
        )
        unexpired_ids = [m.id for m in unexpired]
        assert MemoryId("mem_alpha_01") in unexpired_ids
        assert MemoryId("mem_alpha_expired") not in unexpired_ids

        # Tombstoned record is excluded
        assert MemoryId("mem_alpha_tomb") not in unexpired_ids
    finally:
        conn.close()

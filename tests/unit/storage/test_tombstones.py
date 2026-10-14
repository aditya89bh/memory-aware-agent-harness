"""Unit tests for verifiable forgetting, tombstones, and TTL expiration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from memory_aware_agent_harness.contracts import (
    BranchId,
    CrossNamespaceAccessError,
    MemoryFilter,
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    NamespaceId,
    RecordNotFoundError,
    RunId,
    TombstoneActiveError,
    TombstoneRecord,
)
from memory_aware_agent_harness.storage.connection import create_connection
from memory_aware_agent_harness.storage.memory_repo import (
    get_memory,
    get_tombstone,
    get_tombstones,
    hard_delete_memory,
    is_tombstoned,
    list_active_memories,
    list_tombstoned_memory_ids,
    purge_expired_memories,
    put_memory,
    search_memories,
    tombstone_memory,
)
from memory_aware_agent_harness.storage.migrations import apply_migrations
from memory_aware_agent_harness.validation import (
    BoundaryValidationError,
    validate_tombstone_record,
)


def _init_db() -> sqlite3.Connection:
    conn = create_connection(":memory:")
    apply_migrations(conn)
    return conn


def _make_record(
    mem_id: str,
    *,
    namespace_id: str = "ns_tombstone",
    state: MemoryState = MemoryState.ACTIVE,
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
) -> MemoryRecord:
    ts = created_at or datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    return MemoryRecord(
        id=MemoryId(mem_id),
        namespace_id=NamespaceId(namespace_id),
        kind=MemoryKind.SEMANTIC,
        content=f"Content for {mem_id}",
        state=state,
        confidence=0.9,
        salience=0.8,
        created_at=ts,
        updated_at=ts,
        expires_at=expires_at,
        origin=MemoryOrigin.AGENT_OBSERVATION,
        run_id=RunId("run_1"),
        branch_id=BranchId("main"),
    )


def test_tombstone_record_contract() -> None:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    record = TombstoneRecord(
        tombstone_id="tomb_01",
        target_memory_id=MemoryId("mem_01"),
        namespace_id=NamespaceId("ns_1"),
        reason="User erasure request",
        tombstoned_at=now,
    )
    assert record.tombstone_id == "tomb_01"
    assert record.target_memory_id == MemoryId("mem_01")
    assert record.namespace_id == NamespaceId("ns_1")
    assert record.reason == "User erasure request"
    assert record.tombstoned_at == now

    # Roundtrip serialization
    payload = record.to_dict()
    restored = TombstoneRecord.from_dict(payload)
    assert restored == record

    # Immutability
    with pytest.raises(AttributeError):
        record.reason = "New reason"  # type: ignore[misc]

    # Validation errors
    with pytest.raises(ValueError, match="tombstone_id must not be empty"):
        TombstoneRecord(
            tombstone_id="",
            target_memory_id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_1"),
            reason="reason",
            tombstoned_at=now,
        )

    with pytest.raises(ValueError, match="target_memory_id must not be empty"):
        TombstoneRecord(
            tombstone_id="tomb_1",
            target_memory_id=MemoryId(""),
            namespace_id=NamespaceId("ns_1"),
            reason="reason",
            tombstoned_at=now,
        )

    with pytest.raises(ValueError, match="namespace_id must not be empty"):
        TombstoneRecord(
            tombstone_id="tomb_1",
            target_memory_id=MemoryId("mem_01"),
            namespace_id=NamespaceId(""),
            reason="reason",
            tombstoned_at=now,
        )

    with pytest.raises(ValueError, match="reason must not be empty"):
        TombstoneRecord(
            tombstone_id="tomb_1",
            target_memory_id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_1"),
            reason="  ",
            tombstoned_at=now,
        )

    with pytest.raises(ValueError, match="tombstoned_at must be timezone-aware"):
        TombstoneRecord(
            tombstone_id="tomb_1",
            target_memory_id=MemoryId("mem_01"),
            namespace_id=NamespaceId("ns_1"),
            reason="reason",
            tombstoned_at=datetime(2026, 10, 14, 12, 0, 0),
        )


def test_validate_tombstone_record_boundary() -> None:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    payload = {
        "tombstone_id": "tomb_boundary_01",
        "target_memory_id": "mem_boundary_01",
        "namespace_id": "ns_boundary",
        "reason": "Retention expired",
        "tombstoned_at": now.isoformat(),
    }
    validated = validate_tombstone_record(payload)
    assert isinstance(validated, TombstoneRecord)
    assert validated.tombstone_id == "tomb_boundary_01"

    # Invalid boundary payload
    with pytest.raises(BoundaryValidationError):
        validate_tombstone_record({"tombstone_id": ""})


def test_tombstone_memory_and_get_tombstone() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_tombstone")
    mem_id = MemoryId("mem_alpha")

    rec = _make_record("mem_alpha", namespace_id=str(ns))
    put_memory(conn, rec)

    assert not is_tombstoned(conn, mem_id, ns)
    assert get_tombstone(conn, mem_id, ns) is None

    tomb_now = datetime(2026, 10, 14, 13, 0, 0, tzinfo=UTC)
    tomb_id = tombstone_memory(
        conn,
        mem_id,
        ns,
        "User GDPR erasure request",
        tombstoned_at=tomb_now,
    )
    assert tomb_id == "tomb_mem_alpha"
    assert is_tombstoned(conn, mem_id, ns)

    # get_tombstone returns TombstoneRecord
    tomb_rec = get_tombstone(conn, mem_id, ns)
    assert tomb_rec is not None
    assert tomb_rec.tombstone_id == tomb_id
    assert tomb_rec.target_memory_id == mem_id
    assert tomb_rec.namespace_id == ns
    assert tomb_rec.reason == "User GDPR erasure request"
    assert tomb_rec.tombstoned_at == tomb_now

    # get_memory returns None unless include_tombstoned=True
    assert get_memory(conn, mem_id, ns) is None
    loaded_deleted = get_memory(conn, mem_id, ns, include_tombstoned=True)
    assert loaded_deleted is not None
    assert loaded_deleted.state == MemoryState.DELETED

    # Double tombstone raises TombstoneActiveError
    with pytest.raises(TombstoneActiveError):
        tombstone_memory(conn, mem_id, ns, "Duplicate attempt")


def test_get_tombstones_audit_query_and_namespace_isolation() -> None:
    conn = _init_db()
    ns_a = NamespaceId("ns_a")
    ns_b = NamespaceId("ns_b")

    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    for i in range(3):
        mem_a = MemoryId(f"mem_a_{i}")
        put_memory(conn, _make_record(str(mem_a), namespace_id=str(ns_a)))
        tombstone_memory(
            conn,
            mem_a,
            ns_a,
            f"Audit deletion a_{i}",
            tombstoned_at=now + timedelta(minutes=i),
        )

    mem_b = MemoryId("mem_b_0")
    put_memory(conn, _make_record(str(mem_b), namespace_id=str(ns_b)))
    tombstone_memory(
        conn,
        mem_b,
        ns_b,
        "Audit deletion b_0",
        tombstoned_at=now,
    )

    tombs_a = get_tombstones(conn, ns_a)
    assert len(tombs_a) == 3
    # Ordered by tombstoned_at DESC
    assert tombs_a[0].target_memory_id == MemoryId("mem_a_2")
    assert tombs_a[1].target_memory_id == MemoryId("mem_a_1")
    assert tombs_a[2].target_memory_id == MemoryId("mem_a_0")

    # Limit parameter
    tombs_a_limited = get_tombstones(conn, ns_a, limit=2)
    assert len(tombs_a_limited) == 2

    # Namespace isolation: ns_b only has 1 tombstone
    tombs_b = get_tombstones(conn, ns_b)
    assert len(tombs_b) == 1
    assert tombs_b[0].target_memory_id == MemoryId("mem_b_0")


def test_list_tombstoned_memory_ids_anti_resurrection() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_resurrection")

    rec1 = _make_record("mem_keep", namespace_id=str(ns))
    rec2 = _make_record("mem_purge", namespace_id=str(ns))
    put_memory(conn, rec1)
    put_memory(conn, rec2)

    tombstone_memory(conn, MemoryId("mem_purge"), ns, "Purged memory")

    purged_set = list_tombstoned_memory_ids(conn, ns)
    assert purged_set == frozenset([MemoryId("mem_purge")])
    assert MemoryId("mem_keep") not in purged_set


def test_hard_delete_memory() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_hard_delete")
    mem_id = MemoryId("mem_to_scrub")

    rec = _make_record("mem_to_scrub", namespace_id=str(ns))
    put_memory(conn, rec)

    # Perform hard delete
    now = datetime(2026, 10, 14, 14, 0, 0, tzinfo=UTC)
    tomb_rec = hard_delete_memory(
        conn,
        mem_id,
        ns,
        reason="Physical deletion compliant",
        tombstoned_at=now,
    )

    assert tomb_rec.target_memory_id == mem_id
    assert tomb_rec.namespace_id == ns
    assert tomb_rec.reason == "Physical deletion compliant"

    # Row is completely deleted from memory_records table
    sql = "SELECT * FROM memory_records WHERE memory_id = ?;"
    row = conn.execute(sql, (str(mem_id),)).fetchone()
    assert row is None

    # Even include_tombstoned=True in get_memory returns None
    assert get_memory(conn, mem_id, ns, include_tombstoned=True) is None

    # Tombstone is retained in tombstones table
    assert is_tombstoned(conn, mem_id, ns)
    assert get_tombstone(conn, mem_id, ns) is not None

    # Re-running hard_delete on already deleted/tombstoned memory returns the existing tombstone
    repeat_tomb = hard_delete_memory(conn, mem_id, ns)
    assert repeat_tomb.target_memory_id == mem_id

    # Non-existent record not tombstoned raises RecordNotFoundError
    with pytest.raises(RecordNotFoundError):
        hard_delete_memory(conn, MemoryId("nonexistent"), ns)

    # Cross-namespace access raises CrossNamespaceAccessError
    ns_other = NamespaceId("ns_other")
    put_memory(conn, _make_record("mem_other", namespace_id=str(ns_other)))
    with pytest.raises(CrossNamespaceAccessError):
        hard_delete_memory(conn, MemoryId("mem_other"), ns)


def test_purge_expired_memories_soft_delete() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_purge_soft")
    base_time = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    # rec_expired: expires at base_time - 1 hour
    rec_expired = _make_record(
        "mem_exp",
        namespace_id=str(ns),
        expires_at=base_time - timedelta(hours=1),
        created_at=base_time - timedelta(days=1),
    )
    # rec_valid: expires at base_time + 1 hour
    rec_valid = _make_record(
        "mem_val",
        namespace_id=str(ns),
        expires_at=base_time + timedelta(hours=1),
        created_at=base_time - timedelta(days=1),
    )
    # rec_no_ttl: no expires_at
    rec_no_ttl = _make_record(
        "mem_no_ttl",
        namespace_id=str(ns),
        expires_at=None,
        created_at=base_time - timedelta(days=1),
    )

    put_memory(conn, rec_expired)
    put_memory(conn, rec_valid)
    put_memory(conn, rec_no_ttl)

    purged = purge_expired_memories(conn, ns, as_of=base_time, hard_delete=False)
    assert purged == (MemoryId("mem_exp"),)

    # State updated to EXPIRED
    exp_record = get_memory(conn, MemoryId("mem_exp"), ns, include_tombstoned=True)
    assert exp_record is not None
    assert exp_record.state == MemoryState.EXPIRED

    # Normal get_memory without include_tombstoned returns None for EXPIRED state
    assert get_memory(conn, MemoryId("mem_exp"), ns) is None

    # Valid and no-ttl memories remain active
    val_record = get_memory(conn, MemoryId("mem_val"), ns)
    assert val_record is not None
    assert val_record.state == MemoryState.ACTIVE

    no_ttl_record = get_memory(conn, MemoryId("mem_no_ttl"), ns)
    assert no_ttl_record is not None
    assert no_ttl_record.state == MemoryState.ACTIVE

    # list_active_memories excludes EXPIRED record
    active = list_active_memories(conn, ns, MemoryKind.SEMANTIC)
    active_ids = {r.id for r in active}
    assert MemoryId("mem_exp") not in active_ids
    assert MemoryId("mem_val") in active_ids
    assert MemoryId("mem_no_ttl") in active_ids

    # search_memories excludes EXPIRED record
    q_filter = MemoryFilter(namespace_id=ns, as_of=base_time)
    results = search_memories(conn, q_filter)
    result_ids = {r.id for r in results}
    assert MemoryId("mem_exp") not in result_ids
    assert MemoryId("mem_val") in result_ids


def test_purge_expired_memories_hard_delete() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_purge_hard")
    base_time = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    rec_expired = _make_record(
        "mem_hard_exp",
        namespace_id=str(ns),
        expires_at=base_time - timedelta(minutes=5),
    )
    rec_valid = _make_record(
        "mem_hard_val",
        namespace_id=str(ns),
        expires_at=base_time + timedelta(hours=2),
    )
    put_memory(conn, rec_expired)
    put_memory(conn, rec_valid)

    purged = purge_expired_memories(conn, ns, as_of=base_time, hard_delete=True)
    assert purged == (MemoryId("mem_hard_exp"),)

    # Row deleted from memory_records
    sql = "SELECT * FROM memory_records WHERE memory_id = ?;"
    assert conn.execute(sql, ("mem_hard_exp",)).fetchone() is None

    # Tombstone recorded with TTL_EXPIRED
    tomb = get_tombstone(conn, MemoryId("mem_hard_exp"), ns)
    assert tomb is not None
    assert tomb.reason == "TTL_EXPIRED"

    # Valid memory remains untouched
    assert get_memory(conn, MemoryId("mem_hard_val"), ns) is not None


def test_purge_expired_memories_empty_and_timezone_validation() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_purge_edge")
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    # Purge on empty namespace returns ()
    assert purge_expired_memories(conn, ns, as_of=now) == ()

    # Naive datetime raises ValueError
    naive = datetime(2026, 10, 14, 12, 0, 0)
    with pytest.raises(ValueError, match="as_of datetime must be timezone-aware"):
        purge_expired_memories(conn, ns, as_of=naive)


def test_get_memory_with_as_of_ttl_filtering() -> None:
    conn = _init_db()
    ns = NamespaceId("ns_ttl_get")
    base_time = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    rec = _make_record(
        "mem_ttl_test",
        namespace_id=str(ns),
        expires_at=base_time + timedelta(hours=1),
    )
    put_memory(conn, rec)

    # Query before expiration -> returns record
    before = base_time + timedelta(minutes=30)
    assert get_memory(conn, MemoryId("mem_ttl_test"), ns, as_of=before) is not None

    # Query at or after expiration -> returns None
    after = base_time + timedelta(hours=2)
    assert get_memory(conn, MemoryId("mem_ttl_test"), ns, as_of=after) is None

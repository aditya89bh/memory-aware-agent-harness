"""Unit tests for deterministic multi-factor memory scoring and ranking."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

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
    search_memories_scored,
)
from memory_aware_agent_harness.storage.migrations import apply_migrations
from memory_aware_agent_harness.storage.scoring import (
    PROVENANCE_FACTORS,
    WEIGHT_IMPORTANCE,
    WEIGHT_PROVENANCE,
    WEIGHT_RECENCY,
    WEIGHT_RELEVANCE,
    calculate_recency_score,
    calculate_relevance_score,
    compute_memory_score,
    score_and_rank_memories,
)


def _make_record(
    mem_id: str,
    content: str,
    *,
    salience: float = 0.5,
    origin: MemoryOrigin = MemoryOrigin.AGENT_OBSERVATION,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    namespace_id: str = "ns_scoring",
) -> MemoryRecord:
    ts = created_at or datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    up_ts = updated_at or ts
    return MemoryRecord(
        id=MemoryId(mem_id),
        namespace_id=NamespaceId(namespace_id),
        kind=MemoryKind.SEMANTIC,
        content=content,
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=salience,
        created_at=ts,
        updated_at=up_ts,
        origin=origin,
        run_id=RunId("run_1"),
        branch_id=BranchId("main"),
    )


def test_locked_weights_sum_to_one() -> None:
    total = WEIGHT_RELEVANCE + WEIGHT_RECENCY + WEIGHT_IMPORTANCE + WEIGHT_PROVENANCE
    assert math.isclose(total, 1.0, rel_tol=1e-9)
    assert WEIGHT_RELEVANCE == 0.45
    assert WEIGHT_RECENCY == 0.25
    assert WEIGHT_IMPORTANCE == 0.20
    assert WEIGHT_PROVENANCE == 0.10


def test_provenance_factors_ordering() -> None:
    assert PROVENANCE_FACTORS[MemoryOrigin.SYSTEM_PROVIDED] == 1.0
    assert PROVENANCE_FACTORS[MemoryOrigin.USER_EXPLICIT] == 0.9
    assert PROVENANCE_FACTORS[MemoryOrigin.AGENT_OBSERVATION] == 0.7
    assert PROVENANCE_FACTORS[MemoryOrigin.DERIVED_CONSOLIDATION] == 0.6
    assert PROVENANCE_FACTORS[MemoryOrigin.EXTERNAL_RETRIEVAL] == 0.5


def test_calculate_relevance_score_empty_or_whitespace() -> None:
    assert calculate_relevance_score(None, "sample content") == 1.0
    assert calculate_relevance_score("", "sample content") == 1.0
    assert calculate_relevance_score("   ", "sample content") == 1.0
    assert calculate_relevance_score("!@#$%", "sample content") == 1.0


def test_calculate_relevance_score_lexical_overlap_and_substring() -> None:
    score_full_match = calculate_relevance_score("python postgres", "python and postgres are great")
    # 2 tokens overlap (1.0) + substring bonus (0.0 because not contiguous substring) -> 1.0
    assert score_full_match == 1.0

    score_substring = calculate_relevance_score("fast turnaround", "we need fast turnaround today")
    # 2 of 2 overlap (1.0) + substring (0.2) capped at 1.0 -> 1.0
    assert score_substring == 1.0

    score_partial = calculate_relevance_score("python redis", "python sqlite postgres")
    # 1 of 2 tokens match -> 0.5, no substring -> 0.5
    assert math.isclose(score_partial, 0.5, rel_tol=1e-5)

    score_zero = calculate_relevance_score("unrelated query", "completely different words here")
    assert score_zero == 0.0


def test_calculate_recency_score_decay() -> None:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    # Identical or future updated_at -> 1.0
    assert calculate_recency_score(now, now) == 1.0
    assert calculate_recency_score(now + timedelta(hours=1), now) == 1.0

    # 24 hours elapsed (half-life) -> 0.50
    one_day_ago = now - timedelta(days=1)
    decay_24h = calculate_recency_score(one_day_ago, now)
    assert math.isclose(decay_24h, 0.50, rel_tol=1e-3)

    # 48 hours elapsed (two half-lives) -> 0.25
    two_days_ago = now - timedelta(days=2)
    decay_48h = calculate_recency_score(two_days_ago, now)
    assert math.isclose(decay_48h, 0.25, rel_tol=1e-3)


def test_calculate_recency_score_timezone_validation() -> None:
    aware = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    naive = datetime(2026, 10, 14, 12, 0, 0)

    with pytest.raises(ValueError, match="as_of datetime must be timezone-aware"):
        calculate_recency_score(aware, naive)

    with pytest.raises(ValueError, match="updated_at datetime must be timezone-aware"):
        calculate_recency_score(naive, aware)


def test_compute_memory_score_breakdown() -> None:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    rec = _make_record(
        "mem_1",
        "Python caching with Redis",
        salience=0.8,
        origin=MemoryOrigin.USER_EXPLICIT,
        created_at=now,
        updated_at=now,
    )

    scored = compute_memory_score(rec, "Redis", now)
    assert scored.record == rec
    # relevance: 1.0, recency: 1.0, importance: 0.8, provenance: 0.9
    # 0.45*1.0 + 0.25*1.0 + 0.20*0.8 + 0.10*0.9 = 0.45 + 0.25 + 0.16 + 0.09 = 0.95
    assert math.isclose(scored.score, 0.95, rel_tol=1e-4)
    assert scored.score_breakdown["relevance"] == 1.0
    assert scored.score_breakdown["recency"] == 1.0
    assert scored.score_breakdown["importance"] == 0.8
    assert scored.score_breakdown["provenance"] == 0.9


def test_score_and_rank_memories_deterministic_ordering() -> None:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    rec1 = _make_record(
        "mem_a",
        "Low relevance target",
        salience=0.3,
        origin=MemoryOrigin.EXTERNAL_RETRIEVAL,
        created_at=now,
    )
    rec2 = _make_record(
        "mem_b",
        "High relevance target with python",
        salience=0.9,
        origin=MemoryOrigin.SYSTEM_PROVIDED,
        created_at=now,
    )

    q_filter = MemoryFilter(
        namespace_id=NamespaceId("ns_scoring"),
        query_text="python target",
    )
    ranked = score_and_rank_memories((rec1, rec2), q_filter, as_of=now)
    assert len(ranked) == 2
    assert ranked[0].record.id == MemoryId("mem_b")
    assert ranked[1].record.id == MemoryId("mem_a")
    assert ranked[0].score > ranked[1].score


def test_score_and_rank_memories_tie_breaking() -> None:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    # Two records with identical content, salience, origin, but different created_at
    rec_older = _make_record(
        "mem_older",
        "identical content",
        salience=0.5,
        created_at=now - timedelta(hours=1),
        updated_at=now,
    )
    rec_newer = _make_record(
        "mem_newer",
        "identical content",
        salience=0.5,
        created_at=now,
        updated_at=now,
    )

    q_filter = MemoryFilter(namespace_id=NamespaceId("ns_scoring"))
    ranked = score_and_rank_memories((rec_older, rec_newer), q_filter, as_of=now)
    # Scores are equal (same updated_at), tie-break by created_at DESC -> rec_newer first
    assert ranked[0].record.id == MemoryId("mem_newer")
    assert ranked[1].record.id == MemoryId("mem_older")

    # Tie-break on created_at equal -> id ASC
    rec_id1 = _make_record("mem_1", "identical", salience=0.5, created_at=now, updated_at=now)
    rec_id2 = _make_record("mem_2", "identical", salience=0.5, created_at=now, updated_at=now)
    ranked2 = score_and_rank_memories((rec_id2, rec_id1), q_filter, as_of=now)
    assert ranked2[0].record.id == MemoryId("mem_1")
    assert ranked2[1].record.id == MemoryId("mem_2")


def test_score_and_rank_memories_limit() -> None:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    recs = tuple(_make_record(f"mem_{i}", f"content {i}", created_at=now) for i in range(10))
    q_filter = MemoryFilter(namespace_id=NamespaceId("ns_scoring"), limit=3)
    ranked = score_and_rank_memories(recs, q_filter, as_of=now)
    assert len(ranked) == 3


def test_score_and_rank_memories_fallback_as_of() -> None:
    # When both filter.as_of and kwarg as_of are None, it uses datetime.now(UTC)
    now = datetime.now(UTC)
    rec = _make_record("mem_fallback", "fallback test", created_at=now, updated_at=now)
    q_filter = MemoryFilter(namespace_id=NamespaceId("ns_scoring"), as_of=None)
    ranked = score_and_rank_memories((rec,), q_filter)
    assert len(ranked) == 1
    assert ranked[0].score > 0.0


def test_search_memories_scored_repository_integration() -> None:
    conn = create_connection(":memory:")
    apply_migrations(conn)

    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    rec1 = _make_record(
        "mem_sql_1",
        "SQLite indexing and query planner",
        salience=0.9,
        origin=MemoryOrigin.SYSTEM_PROVIDED,
        created_at=now,
    )
    rec2 = _make_record(
        "mem_sql_2",
        "sqlite indexing for background tasks",
        salience=0.2,
        origin=MemoryOrigin.EXTERNAL_RETRIEVAL,
        created_at=now,
    )
    put_memory(conn, rec1)
    put_memory(conn, rec2)

    q_filter = MemoryFilter(
        namespace_id=NamespaceId("ns_scoring"),
        query_text="sqlite indexing",
    )
    results = search_memories_scored(conn, q_filter, as_of=now)
    assert len(results) == 2
    assert results[0].record.id == MemoryId("mem_sql_1")
    assert results[0].score > results[1].score
    assert "relevance" in results[0].score_breakdown
    assert results[0].score_breakdown["importance"] > results[1].score_breakdown["importance"]
    assert results[0].score_breakdown["provenance"] > results[1].score_breakdown["provenance"]

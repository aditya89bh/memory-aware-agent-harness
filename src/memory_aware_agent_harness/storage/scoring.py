"""Deterministic multi-factor memory scoring and ranking algorithms."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

from memory_aware_agent_harness.contracts import (
    MemoryFilter,
    MemoryOrigin,
    MemoryRecord,
    ScoredMemoryItem,
)

# Locked scoring weights (sum = 1.0)
WEIGHT_RELEVANCE: float = 0.45
WEIGHT_RECENCY: float = 0.25
WEIGHT_IMPORTANCE: float = 0.20
WEIGHT_PROVENANCE: float = 0.10

# Provenance trust factors by origin category
PROVENANCE_FACTORS: dict[MemoryOrigin, float] = {
    MemoryOrigin.SYSTEM_PROVIDED: 1.0,
    MemoryOrigin.USER_EXPLICIT: 0.9,
    MemoryOrigin.AGENT_OBSERVATION: 0.7,
    MemoryOrigin.DERIVED_CONSOLIDATION: 0.6,
    MemoryOrigin.EXTERNAL_RETRIEVAL: 0.5,
}

# Exponential recency decay constant (half-life of 24 hours: 86,400s)
RECENCY_LAMBDA: float = math.log(2) / 86_400.0


def calculate_relevance_score(query: str | None, content: str) -> float:
    """Calculate deterministic lexical relevance score in [0.0, 1.0]."""
    if query is None or not query.strip():
        return 1.0

    query_tokens = set(re.findall(r"\w+", query.lower()))
    if not query_tokens:
        return 1.0

    content_lower = content.lower()
    content_tokens = set(re.findall(r"\w+", content_lower))

    # Overlap ratio: fraction of query terms present in content
    intersection = query_tokens.intersection(content_tokens)
    overlap = len(intersection) / len(query_tokens)

    # Substring bonus if full query appears contiguously
    substring_bonus = 0.2 if query.strip().lower() in content_lower else 0.0

    return min(1.0, overlap + substring_bonus)


def calculate_recency_score(updated_at: datetime, as_of: datetime) -> float:
    """Calculate exponential recency decay score in [0.0, 1.0] with deterministic as_of."""
    if as_of.tzinfo is None:
        raise ValueError("as_of datetime must be timezone-aware")
    if updated_at.tzinfo is None:
        raise ValueError("updated_at datetime must be timezone-aware")

    elapsed_seconds = (as_of - updated_at).total_seconds()
    if elapsed_seconds <= 0.0:
        return 1.0

    decay = math.exp(-RECENCY_LAMBDA * elapsed_seconds)
    return max(0.0, min(1.0, decay))


def compute_memory_score(
    record: MemoryRecord,
    query: str | None,
    as_of: datetime,
) -> ScoredMemoryItem:
    """Compute composite multi-signal score and transparent component breakdown.

    Score = 0.45 * relevance + 0.25 * recency + 0.20 * importance + 0.10 * provenance
    """
    relevance = calculate_relevance_score(query, record.content)
    recency = calculate_recency_score(record.updated_at, as_of)
    importance = float(record.salience)
    provenance = PROVENANCE_FACTORS.get(record.origin, 0.5)

    composite_score = (
        WEIGHT_RELEVANCE * relevance
        + WEIGHT_RECENCY * recency
        + WEIGHT_IMPORTANCE * importance
        + WEIGHT_PROVENANCE * provenance
    )

    breakdown = {
        "relevance": round(relevance, 6),
        "recency": round(recency, 6),
        "importance": round(importance, 6),
        "provenance": round(provenance, 6),
    }

    return ScoredMemoryItem(
        record=record,
        score=round(composite_score, 6),
        score_breakdown=breakdown,
    )


def score_and_rank_memories(
    records: tuple[MemoryRecord, ...],
    query_filter: MemoryFilter,
    *,
    as_of: datetime | None = None,
) -> tuple[ScoredMemoryItem, ...]:
    """Score memory records and rank in descending order of composite score."""
    effective_as_of = (
        query_filter.as_of
        if query_filter.as_of is not None
        else (as_of if as_of is not None else datetime.now(UTC))
    )

    scored_items = [
        compute_memory_score(rec, query_filter.query_text, effective_as_of) for rec in records
    ]

    # Deterministic sorting: highest score first, tie-break by created_at DESC, then id ASC
    scored_items.sort(
        key=lambda item: (
            -item.score,
            -item.record.created_at.timestamp(),
            str(item.record.id),
        )
    )

    return tuple(scored_items[: query_filter.limit])

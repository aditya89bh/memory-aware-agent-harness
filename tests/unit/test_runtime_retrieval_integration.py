"""Unit tests for multi-signal scored memory retrieval and budgeted context (Task 21)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import (
    ContextItem,
    ContextManifest,
    MemoryFilter,
    MemoryId,
    MemoryKind,
    MemoryOrigin,
    MemoryRecord,
    MemoryState,
    MemoryStoreProtocol,
    ModelCallContext,
    ModelDecision,
    ModelFinishReason,
    NamespaceId,
    RunBudgets,
    RunId,
    ScoredMemoryItem,
    StepId,
    StepResult,
    StoragePort,
)
from memory_aware_agent_harness.runtime.adapters import FakeModelAdapter
from memory_aware_agent_harness.runtime.runner import AgentRuntime, compile_execution_prompt


class _StubScoredStorage(StoragePort):
    """Minimal StoragePort stub supporting search_scored for testing."""

    def __init__(self, scored_items: Sequence[ScoredMemoryItem] = ()) -> None:
        self.scored_items = list(scored_items)
        self.recorded_filters: list[MemoryFilter] = []
        self.saved_runs: list[Any] = []
        self.saved_steps: list[Any] = []

    def search_scored(self, query_filter: MemoryFilter) -> Sequence[ScoredMemoryItem]:
        self.recorded_filters.append(query_filter)
        return self.scored_items

    def save_run(self, run_result: Any) -> None:
        self.saved_runs.append(run_result)

    def save_step(self, step_result: Any, namespace_id: NamespaceId) -> None:
        self.saved_steps.append((step_result, namespace_id))

    def record_step_start(self, *args: Any, **kwargs: Any) -> None:
        pass

    def put(self, record: MemoryRecord) -> None:
        pass

    def get(self, memory_id: MemoryId, namespace_id: NamespaceId) -> MemoryRecord | None:
        return None

    def search(
        self,
        namespace_id: NamespaceId,
        query: str,
        kinds: Sequence[MemoryKind],
        limit: int,
    ) -> Sequence[MemoryRecord]:
        return ()

    def list_active(self, namespace_id: NamespaceId, kind: MemoryKind) -> Sequence[MemoryRecord]:
        return ()

    def tombstone(self, memory_id: MemoryId, namespace_id: NamespaceId, reason: str) -> None:
        pass

    def update_optimistic(self, record: MemoryRecord, expected_version: int) -> None:
        pass

    def get_run(self, run_id: Any, namespace_id: NamespaceId) -> Any | None:
        return None

    def record_tool_intent(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_tool_result(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_tool_invocation(self, *args: Any, **kwargs: Any) -> Any | None:
        return None

    def save_checkpoint(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_checkpoint(
        self,
        checkpoint_id: Any,
        namespace_id: NamespaceId,
    ) -> Mapping[str, Any] | None:
        return None

    def get_latest_checkpoint(self, *args: Any, **kwargs: Any) -> Any | None:
        return None

    def append_trace_event(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_trace_event(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_trace_events(self, *args: Any, **kwargs: Any) -> Sequence[Any]:
        return ()


class _StubFallbackMemoryStore(MemoryStoreProtocol):
    """Basic MemoryStoreProtocol implementing search without search_scored."""

    def __init__(self, records: Sequence[MemoryRecord] = ()) -> None:
        self.records = list(records)

    def put(self, record: MemoryRecord) -> None:
        self.records.append(record)

    def get(self, memory_id: MemoryId, namespace_id: NamespaceId) -> MemoryRecord | None:
        for r in self.records:
            if r.id == memory_id and r.namespace_id == namespace_id:
                return r
        return None

    def search(
        self,
        namespace_id: NamespaceId,
        query: str,
        kinds: Sequence[MemoryKind],
        limit: int,
    ) -> Sequence[MemoryRecord]:
        return [r for r in self.records if r.namespace_id == namespace_id and r.kind in kinds][
            :limit
        ]

    def tombstone(self, memory_id: MemoryId, namespace_id: NamespaceId, reason: str) -> None:
        return None

    def list_active(self, namespace_id: NamespaceId, kind: MemoryKind) -> Sequence[MemoryRecord]:
        return ()


def _make_record(
    mem_id: str,
    content: str,
    origin: MemoryOrigin = MemoryOrigin.USER_EXPLICIT,
    kind: MemoryKind = MemoryKind.WORKING,
) -> MemoryRecord:
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    return MemoryRecord(
        id=MemoryId(mem_id),
        namespace_id=NamespaceId("ns_test"),
        kind=kind,
        content=content,
        state=MemoryState.ACTIVE,
        confidence=0.9,
        salience=0.8,
        created_at=now,
        updated_at=now,
        origin=origin,
    )


def _make_scored_item(mem_id: str, content: str, score: float) -> ScoredMemoryItem:
    rec = _make_record(mem_id, content)
    return ScoredMemoryItem(
        record=rec,
        score=score,
        score_breakdown={
            "lexical_similarity": score,
            "recency_weight": 1.0,
            "salience_weight": 1.0,
            "final_score": score,
        },
    )


def test_compile_execution_prompt_with_context_items() -> None:
    """compile_execution_prompt renders Relevant Context section formatted properly."""
    ctx_items = [
        ContextItem(
            memory_id=MemoryId("mem_1"),
            kind=MemoryKind.WORKING,
            content="User prefers strict type validation",
            rank_score=0.95,
            provenance_id="user_explicit",
            token_cost=5,
        ),
        ContextItem(
            memory_id=MemoryId("mem_2"),
            kind=MemoryKind.SEMANTIC,
            content="SQLite WAL mode guarantees serializable readers",
            rank_score=0.82,
            provenance_id="system_provided",
            token_cost=6,
        ),
    ]

    # No history, with context items
    prompt_no_hist = compile_execution_prompt(
        "Analyze database engine",
        (),
        context_items=ctx_items,
    )
    assert "Task: Analyze database engine" in prompt_no_hist
    assert "--- Relevant Context ---" in prompt_no_hist
    assert "[WORKING] (score: 0.95) User prefers strict type validation" in prompt_no_hist
    assert (
        "[SEMANTIC] (score: 0.82) SQLite WAL mode guarantees serializable readers" in prompt_no_hist
    )
    assert "--- Execution History ---" not in prompt_no_hist

    # With history and context items
    dummy_decision = ModelDecision(
        decision_id="dec_1",
        step_id=StepId("step_1"),
        finish_reason=ModelFinishReason.TOOL_CALL,
        content="Inspected engine configuration",
        tokens_used=10,
    )
    step_result = StepResult(
        step_id=StepId("step_1"),
        run_id=RunId("run_1"),
        step_number=1,
        decision=dummy_decision,
        tool_results=(),
        memories_proposed=0,
        memories_committed=0,
        tokens_consumed=10,
        completed_at=datetime(2026, 10, 15, 12, 0, 1, tzinfo=UTC),
    )
    prompt_with_hist = compile_execution_prompt(
        "Analyze database engine",
        [step_result],
        context_items=ctx_items,
    )
    assert "--- Relevant Context ---" in prompt_with_hist
    assert "--- Execution History ---" in prompt_with_hist
    assert "Inspected engine configuration" in prompt_with_hist


def test_runtime_retrieval_integration_multi_signal_scored() -> None:
    """AgentRuntime executes multi-signal scored retrieval and populates ContextManifest."""
    scored_items = [
        _make_scored_item("m_low", "Low relevance memory fact", 0.35),
        _make_scored_item("m_high", "High relevance memory fact", 0.98),
        _make_scored_item("m_mid", "Medium relevance memory fact", 0.72),
    ]
    storage = _StubScoredStorage(scored_items=scored_items)

    captured_manifests: list[ContextManifest] = []

    def mock_model(
        manifest: ContextManifest,
        tools: Sequence[Mapping[str, Any]],
        ctx: ModelCallContext,
    ) -> ModelDecision:
        captured_manifests.append(manifest)
        return ModelDecision(
            decision_id="dec_finish",
            step_id=ctx.step_id,
            finish_reason=ModelFinishReason.STOP,
            content="Task completed with context",
            tokens_used=15,
        )

    model = FakeModelAdapter(default_handler=mock_model)
    runtime = AgentRuntime(model=model, storage=storage)

    result = runtime.run("Implement scored retrieval", namespace_id=NamespaceId("ns_test"))
    assert result.status.value == "completed"
    assert len(captured_manifests) == 1

    manifest = captured_manifests[0]
    # Items should be ordered descending by score: m_high (0.98), m_mid (0.72), m_low (0.35)
    assert len(manifest.included_items) == 3
    assert manifest.included_items[0].memory_id == MemoryId("m_high")
    assert manifest.included_items[0].rank_score == 0.98
    assert manifest.included_items[1].memory_id == MemoryId("m_mid")
    assert manifest.included_items[2].memory_id == MemoryId("m_low")

    # Verify provenance_id is retained from record origin
    assert manifest.included_items[0].provenance_id == "user_explicit"

    # Verify filter parameters passed to search_scored
    assert len(storage.recorded_filters) == 1
    query_filter = storage.recorded_filters[0]
    assert query_filter.namespace_id == NamespaceId("ns_test")
    assert query_filter.state == MemoryState.ACTIVE
    assert query_filter.query_text == "Implement scored retrieval"
    assert query_filter.limit == 5


def test_runtime_context_budget_exclusion() -> None:
    """AgentRuntime excludes lower-scored items exceeding budget and records BUDGET_EXCEEDED."""
    scored_items = [
        _make_scored_item("m_prio1", "Word " * 5, 0.95),  # cost = 5
        _make_scored_item("m_prio2", "Word " * 5, 0.85),  # cost = 5
        _make_scored_item("m_prio3", "Word " * 10, 0.70),  # cost = 10
    ]
    storage = _StubScoredStorage(scored_items=scored_items)

    captured_manifests: list[ContextManifest] = []

    def mock_model(
        manifest: ContextManifest,
        tools: Sequence[Mapping[str, Any]],
        ctx: ModelCallContext,
    ) -> ModelDecision:
        captured_manifests.append(manifest)
        return ModelDecision(
            decision_id="dec_finish",
            step_id=ctx.step_id,
            finish_reason=ModelFinishReason.STOP,
            content="Done",
            tokens_used=10,
        )

    model = FakeModelAdapter(default_handler=mock_model)
    # Set tight token budget: base prompt for "Test task" is ~2 tokens.
    # Total max_tokens = 14 => available for context = 14 - 2 = 12.
    # m_prio1 (5) + m_prio2 (5) = 10 <= 12.
    # m_prio3 requires 10, 10 + 10 = 20 > 12 -> excluded.
    runtime = AgentRuntime(
        model=model,
        storage=storage,
        budgets=RunBudgets(max_tokens=14, max_steps=5),
    )

    result = runtime.run("Test task", namespace_id=NamespaceId("ns_test"))
    assert result.status.value == "completed"
    assert len(captured_manifests) == 1

    manifest = captured_manifests[0]
    included_ids = [item.memory_id for item in manifest.included_items]
    assert MemoryId("m_prio1") in included_ids
    assert MemoryId("m_prio2") in included_ids
    assert MemoryId("m_prio3") not in included_ids

    # Verify excluded item is marked with BUDGET_EXCEEDED
    assert MemoryId("m_prio3") in manifest.excluded_item_ids
    assert manifest.excluded_item_ids[MemoryId("m_prio3")] == "BUDGET_EXCEEDED"


def test_runtime_fallback_to_unscored_memory_store() -> None:
    """AgentRuntime falls back gracefully to standard search when storage lacks search_scored."""
    record = _make_record("mem_legacy", "Legacy memory fact")
    mem_store = _StubFallbackMemoryStore(records=[record])

    captured_manifests: list[ContextManifest] = []

    def mock_model(
        manifest: ContextManifest,
        tools: Sequence[Mapping[str, Any]],
        ctx: ModelCallContext,
    ) -> ModelDecision:
        captured_manifests.append(manifest)
        return ModelDecision(
            decision_id="dec_finish",
            step_id=ctx.step_id,
            finish_reason=ModelFinishReason.STOP,
            content="Done",
            tokens_used=5,
        )

    model = FakeModelAdapter(default_handler=mock_model)
    runtime = AgentRuntime(model=model, memory_store=mem_store)

    result = runtime.run("Retrieve legacy", namespace_id=NamespaceId("ns_test"))
    assert result.status.value == "completed"
    assert len(captured_manifests) == 1

    manifest = captured_manifests[0]
    assert len(manifest.included_items) == 1
    assert manifest.included_items[0].memory_id == MemoryId("mem_legacy")
    assert manifest.included_items[0].content == "Legacy memory fact"

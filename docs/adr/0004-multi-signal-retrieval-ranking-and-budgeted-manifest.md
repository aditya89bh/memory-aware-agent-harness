# ADR-0004: Multi-Signal Retrieval Ranking and Budgeted Context Manifest

## Status
Accepted

## Context
Standard RAG frameworks rely almost exclusively on semantic vector similarity. In autonomous agents, this leads to severe failure modes: stale or outdated memories are frequently retrieved because they are semantically similar to the query, while important recent facts, frequency bonuses, and intrinsic salience are ignored. Furthermore, blindly appending retrieved chunks risks prompt bloat and truncation errors.

## Decision
We implement a **Multi-Signal Retrieval Scoring Function** and a **Budgeted Context Compiler**:
1. Scoring computes a weighted sum of: Lexical/BM25 relevance, exponential recency decay, intrinsic salience, and logarithmic access frequency, minus a decay penalty for nearing TTL expiration.
2. The context compiler partitions prompt tokens into reserved bands (system prompt, tool schemas, task input, working memory) and dynamic bands (semantic, episodic, procedural).
3. Every prompt assembly produces an immutable `ContextManifest` recording all included items, their rank scores, and every excluded item with an explicit exclusion reason (`EXCLUDED_BUDGET_EXCEEDED`, `EXCLUDED_LOW_RELEVANCE`).
4. Retrieved content is fenced in untrusted XML wrappers (`<untrusted_memory>`) to mitigate prompt injection.

## Consequences
- **Positive:** Context selection is transparent, auditable, and resilient against stale memory; hard token budgets prevent context overruns.
- **Negative:** Linear combination of weights requires parameter tuning for specific domains.
- **Reversal Trigger:** If an external learned re-ranker demonstrably outperforms multi-signal scoring across all latency and cost constraints.

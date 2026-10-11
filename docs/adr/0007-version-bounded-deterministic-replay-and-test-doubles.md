# ADR-0007: Version-Bounded Deterministic Replay and First-Class Test Doubles

## Status
Accepted

## Context
Validating memory agent behavior and running reproducible regression tests is challenging when executions depend on external non-deterministic LLMs, system clocks, random number generators, or network tools. Furthermore, claiming that an agent can replay an execution weeks later using a live LLM is technically unsound because models, system prompts, and tool endpoints change.

## Decision
We define an explicit **Deterministic Replay Contract** supported by first-class test doubles:
1. Replay determinism is strictly guaranteed for **canonical recorded inputs and outputs under compatible schema and policy versions**.
2. The core harness ships with deterministic test doubles:
   - `DeterministicClock`: Virtual clock advanceable deterministically.
   - `SeedableRNG`: Deterministic pseudo-random generation for IDs and ranking noise.
   - `FakeModelAdapter`: Deterministic scripted responses and decisions.
   - `StubToolDispatcher`: Intercepts and replays recorded tool observations.
3. Replaying against live external LLMs or third-party tools is explicitly documented as non-deterministic and out of scope.
4. Divergence detection automatically compares replayed state hashes against recorded checkpoint hashes.

## Consequences
- **Positive:** Comprehensive test and benchmark execution in CI completely offline without API keys in seconds; clear, honest replay guarantees.
- **Negative:** Replay does not guarantee that a live external model will repeat prior behavior.
- **Reversal Trigger:** None; core testing and reproducibility invariant.

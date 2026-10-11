# ADR-0010: Deep Immutability, Normalization, and Canonical Serialization

## Status
Accepted

## Context
Python dataclasses decorated with `@dataclass(frozen=True)` provide only shallow immutability if their attributes contain mutable collection types such as `list` or `dict`. A caller can freely execute `record.source_references.append(...)` or `record.metadata['key'] = 'value'`, or mutate the input structures passed to the constructor after instantiation, mutating internal state and causing concurrency bugs, cache invalidation failures, and broken trace provenance. Furthermore, serializing and deserializing records across processes requires canonical, reproducible serialization without field drift.

## Decision
We enforce **Deep Immutability, Normalization, and Canonical Serialization** across all public data contracts:
1. All sequence fields in public frozen dataclasses use immutable `Tuple[T, ...]` instead of `List[T]`.
2. All dictionary/mapping fields use `Mapping[str, Any]` backed by `types.MappingProxyType` or recursively frozen mapping implementations.
3. Constructors and `__post_init__` hooks recursively normalize collections (freezing mappings into nested `MappingProxyType` and sequences into `tuple`), defending against alias mutations from caller references.
4. All public records inherit from `SerializableRecord`, providing canonical `to_dict()`, `from_dict()`, `to_json()`, and `from_json()` methods with deterministic sorted keys, compact RFC separators, and ISO-8601 UTC timestamp conversions.

## Consequences
- **Positive:** Genuine immutability guarantees; immunity to alias mutations; reproducible canonical serialization; thread-safe caching.
- **Negative:** Minor overhead during initial object construction due to recursive freezing.
- **Reversal Trigger:** None; essential for reliable event tracing and state snapshotting.

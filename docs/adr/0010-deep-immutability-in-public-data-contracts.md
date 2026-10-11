# ADR-0010: Deep Immutability in Public Data Contracts

## Status
Accepted

## Context
Python dataclasses decorated with `@dataclass(frozen=True)` provide only shallow immutability if their attributes contain mutable collection types such as `list` or `dict`. A caller can freely execute `record.source_references.append(...)` or `record.metadata['key'] = 'value'`, mutating internal state and causing subtle concurrency bugs, cache invalidation failures, and broken trace provenance.

## Decision
We enforce **Deep Immutability** across all public data contracts:
1. All sequence fields in public frozen dataclasses use immutable `Tuple[T, ...]` instead of `List[T]`.
2. All dictionary/mapping fields use `Mapping[str, Any]` backed by `types.MappingProxyType` or frozen mapping implementations.
3. Constructors and factory functions automatically convert incoming mutable lists or dictionaries to tuples and mapping proxies during initialization.

## Consequences
- **Positive:** True immutability guarantees; records are safely hashable, cacheable, and thread-safe; eliminates side-channel mutations.
- **Negative:** Slightly more verbose instantiation when constructing records with collections (must pass tuples or convert).
- **Reversal Trigger:** None; essential for reliable event tracing and state snapshotting.

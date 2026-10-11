"""Deterministic test doubles and simulation adapters for replay and testing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memory_aware_agent_harness.contracts import ClockPort, IdGeneratorPort


class DeterministicClock(ClockPort):
    """Deterministic, controllable clock double implementing ClockPort."""

    def __init__(self, start_time: datetime | None = None) -> None:
        if start_time is None:
            self._current_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        else:
            if start_time.tzinfo is None:
                raise ValueError("start_time must be timezone-aware (tzinfo is required)")
            self._current_time = start_time

    def now(self) -> datetime:
        """Return the current frozen/simulated UTC datetime."""
        return self._current_time

    def advance(self, duration: float | timedelta) -> datetime:
        """Advance the simulated clock by seconds (float) or timedelta."""
        delta = duration if isinstance(duration, timedelta) else timedelta(seconds=duration)
        if delta.total_seconds() < 0:
            raise ValueError(f"Clock cannot move backwards, got duration {duration}")
        self._current_time += delta
        return self._current_time

    def set_time(self, dt: datetime) -> None:
        """Explicitly set the clock time to a specific timezone-aware datetime."""
        if dt.tzinfo is None:
            raise ValueError("Target datetime must be timezone-aware (tzinfo is required)")
        self._current_time = dt


class DeterministicIdGenerator(IdGeneratorPort):
    """Deterministic, sequential identifier generator implementing IdGeneratorPort."""

    def __init__(self, prefix: str = "", start_counter: int = 1) -> None:
        if start_counter < 1:
            raise ValueError(f"start_counter must be >= 1, got {start_counter}")
        self._default_prefix = prefix
        self._counter = start_counter

    def generate_id(self, prefix: str = "") -> str:
        """Generate a sequential, deterministic ID formatted as '{prefix}{counter:06d}'."""
        effective_prefix = prefix if prefix else self._default_prefix
        current_val = self._counter
        self._counter += 1
        if effective_prefix:
            sep = "" if effective_prefix.endswith(("_", "-")) else "_"
            return f"{effective_prefix}{sep}{current_val:06d}"
        return f"{current_val:06d}"

    def reset(self, start_counter: int = 1) -> None:
        """Reset the internal sequence counter."""
        if start_counter < 1:
            raise ValueError(f"start_counter must be >= 1, got {start_counter}")
        self._counter = start_counter


__all__ = [
    "DeterministicClock",
    "DeterministicIdGenerator",
]

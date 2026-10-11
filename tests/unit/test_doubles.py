"""Unit tests for deterministic simulation test doubles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memory_aware_agent_harness.contracts import ClockPort, IdGeneratorPort
from memory_aware_agent_harness.doubles import DeterministicClock, DeterministicIdGenerator


def test_deterministic_clock_advancement_and_validation() -> None:
    """DeterministicClock provides precise, controlled time advancing and validates tzinfo."""
    clock = DeterministicClock()
    assert isinstance(clock, ClockPort)
    assert clock.now().tzinfo is not None

    # Advance by float seconds
    t0 = clock.now()
    t1 = clock.advance(60.5)
    assert t1 == t0 + timedelta(seconds=60.5)
    assert clock.now() == t1

    # Advance by timedelta
    t2 = clock.advance(timedelta(minutes=5))
    assert t2 == t1 + timedelta(minutes=5)

    # Disallow backward time jumps via advance
    with pytest.raises(ValueError, match=r"Clock cannot move backwards"):
        clock.advance(-1.0)

    # Set explicit time
    target = datetime(2027, 5, 20, 10, 0, 0, tzinfo=UTC)
    clock.set_time(target)
    assert clock.now() == target

    # Reject naive datetime in set_time
    naive = datetime(2027, 5, 20, 10, 0, 0)
    with pytest.raises(ValueError, match=r"Target datetime must be timezone-aware"):
        clock.set_time(naive)

    # Reject naive datetime in constructor
    with pytest.raises(ValueError, match=r"start_time must be timezone-aware"):
        DeterministicClock(start_time=naive)


def test_deterministic_id_generator_sequence_and_prefix() -> None:
    """DeterministicIdGenerator produces predictable monotonically increasing identifiers."""
    gen = DeterministicIdGenerator(prefix="test")
    assert isinstance(gen, IdGeneratorPort)

    id1 = gen.generate_id()
    id2 = gen.generate_id()
    assert id1 == "test_000001"
    assert id2 == "test_000002"

    # Override prefix per call
    id3 = gen.generate_id(prefix="mem_")
    id4 = gen.generate_id(prefix="step-")
    assert id3 == "mem_000003"
    assert id4 == "step-000004"

    # Reset
    gen.reset(start_counter=1)
    assert gen.generate_id() == "test_000001"

    # Constructor and reset validation
    with pytest.raises(ValueError, match=r"start_counter must be >= 1"):
        DeterministicIdGenerator(start_counter=0)

    with pytest.raises(ValueError, match=r"start_counter must be >= 1"):
        gen.reset(start_counter=-5)


def test_deterministic_id_generator_without_prefix() -> None:
    """DeterministicIdGenerator without prefix produces raw zero-padded integers."""
    gen = DeterministicIdGenerator()
    assert gen.generate_id() == "000001"
    assert gen.generate_id() == "000002"

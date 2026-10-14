"""Unit tests for SQLite schema DDL and versioned migrations."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from memory_aware_agent_harness.storage.connection import create_connection
from memory_aware_agent_harness.storage.migrations import (
    MIGRATIONS,
    Migration,
    MigrationError,
    apply_migrations,
    get_current_schema_version,
)

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "schema_migrations",
        "runs",
        "run_steps",
        "memory_records",
        "memory_lineage",
        "tombstones",
        "tool_invocations",
        "checkpoints",
        "trace_events",
    }
)


def test_apply_migrations_creates_all_tables_and_indexes() -> None:
    """apply_migrations creates all 8 core entities and indexes cleanly."""
    conn = create_connection(":memory:")
    try:
        assert get_current_schema_version(conn) == 0

        version = apply_migrations(conn)
        assert version == 1
        assert get_current_schema_version(conn) == 1

        # Verify all expected tables exist
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
        ).fetchall()
        created_tables = {r[0] for r in rows}
        assert EXPECTED_TABLES.issubset(created_tables)

        # Verify critical composite and partial indexes exist
        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%';"
        ).fetchall()
        indexes = {r[0] for r in idx_rows}
        assert "idx_runs_namespace" in indexes
        assert "idx_run_steps_namespace_run" in indexes
        assert "idx_memories_active" in indexes
        assert "idx_memories_lookup" in indexes
        assert "idx_lineage_target" in indexes
        assert "idx_tombstones_lookup" in indexes
        assert "idx_tool_idempotency" in indexes
        assert "idx_checkpoints_lookup" in indexes
        assert "idx_events_sequence" in indexes
        assert "idx_events_namespace" in indexes
    finally:
        conn.close()


def test_apply_migrations_is_idempotent() -> None:
    """Calling apply_migrations repeatedly produces no duplicate errors or version changes."""
    conn = create_connection(":memory:")
    try:
        v1 = apply_migrations(conn)
        v2 = apply_migrations(conn)
        assert v1 == 1
        assert v2 == 1
    finally:
        conn.close()


def test_migration_checksum_tamper_detection() -> None:
    """Tampering with recorded migration checksum raises MigrationError."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)

        # Tamper with recorded checksum
        conn.execute("UPDATE schema_migrations SET checksum = 'tampered_hash' WHERE version = 1;")

        with pytest.raises(MigrationError, match=r"Checksum mismatch for migration version 1"):
            apply_migrations(conn)
    finally:
        conn.close()


def test_migration_failure_rolls_back_atomically() -> None:
    """Failing SQL statement inside a migration causes complete atomic rollback."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)

        bad_migration = Migration(
            version=2,
            description="Bad migration",
            up_sql="CREATE TABLE valid_tbl (id INT); INVALID SQL SYNTAX HERE;",
        )

        with (
            patch(
                "memory_aware_agent_harness.storage.migrations.MIGRATIONS",
                (*MIGRATIONS, bad_migration),
            ),
            pytest.raises(MigrationError, match=r"Failed executing migration 2"),
        ):
            apply_migrations(conn)

        # Version remains 1
        assert get_current_schema_version(conn) == 1

        # Table created in failed migration was rolled back
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'valid_tbl';"
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_target_version_bounds() -> None:
    """Target version limits which migrations are applied."""
    conn = create_connection(":memory:")
    try:
        # Target version 0 applies no migrations
        v = apply_migrations(conn, target_version=0)
        assert v == 0
        assert get_current_schema_version(conn) == 0

        # Then apply to target 1
        v1 = apply_migrations(conn, target_version=1)
        assert v1 == 1
        assert get_current_schema_version(conn) == 1
    finally:
        conn.close()

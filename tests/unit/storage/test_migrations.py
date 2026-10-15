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
        assert version == 2
        assert get_current_schema_version(conn) == 2

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
        assert "idx_tool_fingerprint" in indexes
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
        assert v1 == 2
        assert v2 == 2
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


def test_upgrade_from_a371da3_database() -> None:
    """A database created at a371da3 (v1 only) seamlessly upgrades to v2 with data preserved."""
    conn = create_connection(":memory:")
    try:
        # 1. Setup exact a371da3 database state: migration 1 recorded with expected checksum
        v1_checksum = "124cb7853b91c22603c79802e1a9bb03b2171d16be3e7e14b8e743f018fce8ab"
        v1_applied = apply_migrations(conn, target_version=1)
        assert v1_applied == 1
        assert get_current_schema_version(conn) == 1

        # Verify recorded checksum matches a371da3 baseline
        row = conn.execute("SELECT checksum FROM schema_migrations WHERE version = 1;").fetchone()
        assert row is not None
        assert row[0] == v1_checksum

        # 2. Insert test data in v1 schema
        conn.execute(
            """
            INSERT INTO runs (
                run_id, session_id, namespace_id, status, max_steps, token_budget, created_at
            ) VALUES (
                'run_compat', 'sess_1', 'ns_compat', 'running', 10, 1000,
                '2026-10-15T00:00:00+00:00'
            );
            """
        )
        conn.execute(
            """
            INSERT INTO run_steps (step_id, run_id, namespace_id, step_number, state, started_at)
            VALUES (
                'step_compat_1', 'run_compat', 'ns_compat', 1, 'executing',
                '2026-10-15T00:00:01+00:00'
            );
            """
        )
        conn.execute(
            """
            INSERT INTO tool_invocations (
                invocation_id, step_id, namespace_id, tool_name, arguments_json,
                side_effect, idempotency_key, status, dispatched_at
            ) VALUES (
                'inv_compat_1', 'step_compat_1', 'ns_compat', 'calc', '{"op":"add"}',
                'read_only', 'key_1', 'completed', '2026-10-15T00:00:02+00:00'
            );
            """
        )

        # 3. Apply forward migrations up to latest (v2)
        v2_applied = apply_migrations(conn)
        assert v2_applied == 2
        assert get_current_schema_version(conn) == 2

        # 4. Verify data preservation and v2 schema changes
        inv_row = conn.execute(
            """
            SELECT invocation_id, tool_name, arguments_json, argument_fingerprint
            FROM tool_invocations WHERE invocation_id = 'inv_compat_1';
            """
        ).fetchone()
        assert inv_row is not None
        assert inv_row["invocation_id"] == "inv_compat_1"
        assert inv_row["tool_name"] == "calc"
        assert inv_row["arguments_json"] == '{"op":"add"}'
        # New column has default value
        assert inv_row["argument_fingerprint"] == ""

        # 5. Repeated migration is idempotent
        v_repeat = apply_migrations(conn)
        assert v_repeat == 2
    finally:
        conn.close()


def test_unsupported_future_schema_version_rejected() -> None:
    """Opening a database with a schema version newer than supported raises MigrationError."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)

        # Simulate database touched by future software with version 99
        conn.execute(
            """
            INSERT INTO schema_migrations (version, applied_at, checksum, description)
            VALUES (99, '2027-01-01T00:00:00+00:00', 'future_hash', 'Future schema');
            """
        )

        with pytest.raises(MigrationError, match=r"newer than supported version"):
            apply_migrations(conn)
    finally:
        conn.close()


def test_migration_failure_rolls_back_atomically() -> None:
    """Failing SQL statement inside a migration causes complete atomic rollback."""
    conn = create_connection(":memory:")
    try:
        apply_migrations(conn)
        assert get_current_schema_version(conn) == 2

        bad_migration = Migration(
            version=3,
            description="Bad migration",
            up_sql="CREATE TABLE valid_tbl (id INT); INVALID SQL SYNTAX HERE;",
        )

        with (
            patch(
                "memory_aware_agent_harness.storage.migrations.MIGRATIONS",
                (*MIGRATIONS, bad_migration),
            ),
            pytest.raises(MigrationError, match=r"Failed executing migration 3"),
        ):
            apply_migrations(conn)

        # Version remains 2
        assert get_current_schema_version(conn) == 2

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

        # Then apply to target 2
        v2 = apply_migrations(conn, target_version=2)
        assert v2 == 2
        assert get_current_schema_version(conn) == 2
    finally:
        conn.close()

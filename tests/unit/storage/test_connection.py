"""Unit tests for SQLite connection factory and transaction management."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memory_aware_agent_harness.storage.connection import (
    StorageSettings,
    create_connection,
    transaction,
)


def test_storage_settings_validation() -> None:
    """StorageSettings validates timeout and synchronous mode."""
    with pytest.raises(ValueError, match=r"timeout_seconds must be > 0\.0"):
        StorageSettings(timeout_seconds=0.0)

    with pytest.raises(ValueError, match=r"synchronous must be one of"):
        StorageSettings(synchronous="INVALID_MODE")

    valid = StorageSettings(synchronous="normal")
    assert valid.synchronous == "NORMAL"


def test_create_connection_memory_pragmas() -> None:
    """In-memory connection applies foreign_keys, secure_delete, and busy_timeout."""
    conn = create_connection(":memory:", timeout_seconds=3.0, synchronous="NORMAL")
    try:
        assert conn.row_factory is sqlite3.Row

        fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert fk == 1

        sd = conn.execute("PRAGMA secure_delete;").fetchone()[0]
        assert sd == 1

        bt = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert bt == 3000

        sync = conn.execute("PRAGMA synchronous;").fetchone()[0]
        # NORMAL is integer 1 in SQLite
        assert sync == 1
    finally:
        conn.close()


def test_create_connection_file_wal(tmp_path: Path) -> None:
    """File-backed connection enables WAL mode and creates parent directories."""
    db_file = tmp_path / "subdir" / "agent_test.db"
    conn = create_connection(db_file, timeout_seconds=4.0)
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"
        assert db_file.exists()
    finally:
        conn.close()


def test_foreign_keys_constraint_enforcement() -> None:
    """Foreign keys are strictly enforced on table inserts."""
    conn = create_connection(":memory:")
    try:
        conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY);")
        conn.execute(
            "CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES parent(id));"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO child VALUES ('c1', 'missing_parent');")
    finally:
        conn.close()


def test_transaction_commit_and_rollback() -> None:
    """transaction context manager commits on success and rolls back on exception."""
    conn = create_connection(":memory:")
    try:
        conn.execute("CREATE TABLE items (id INT PRIMARY KEY, name TEXT);")

        with transaction(conn):
            conn.execute("INSERT INTO items VALUES (1, 'one');")

        row = conn.execute("SELECT name FROM items WHERE id = 1;").fetchone()
        assert row["name"] == "one"

        with pytest.raises(RuntimeError), transaction(conn):
            conn.execute("INSERT INTO items VALUES (2, 'two');")
            raise RuntimeError("Abort")

        assert conn.execute("SELECT COUNT(*) FROM items;").fetchone()[0] == 1
    finally:
        conn.close()


def test_nested_transaction_savepoints() -> None:
    """Nested transactions use savepoints allowing partial rollback."""
    conn = create_connection(":memory:")
    try:
        conn.execute("CREATE TABLE items (id INT PRIMARY KEY, name TEXT);")

        with transaction(conn):
            conn.execute("INSERT INTO items VALUES (1, 'one');")

            # Nested transaction succeeds
            with transaction(conn):
                conn.execute("INSERT INTO items VALUES (2, 'two');")

            # Nested transaction fails and rolls back
            try:
                with transaction(conn):
                    conn.execute("INSERT INTO items VALUES (3, 'three');")
                    raise ValueError("Nested failure")
            except ValueError:
                pass

            conn.execute("INSERT INTO items VALUES (4, 'four');")

        rows = conn.execute("SELECT id FROM items ORDER BY id;").fetchall()
        assert [r["id"] for r in rows] == [1, 2, 4]
    finally:
        conn.close()

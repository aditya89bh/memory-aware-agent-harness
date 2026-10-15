"""Concurrency tests for SQLite busy timeout, lock contention, and retry behavior."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from memory_aware_agent_harness.storage.connection import create_connection, transaction


def test_busy_timeout_pragma_applied(tmp_path: Path) -> None:
    """Verifies PRAGMA busy_timeout is set correctly from timeout_seconds."""
    db_file = tmp_path / "busy_pragma.db"
    conn = create_connection(db_file, timeout_seconds=3.5)
    try:
        row = conn.execute("PRAGMA busy_timeout;").fetchone()
        assert row is not None
        assert row[0] == 3500
    finally:
        conn.close()


def test_busy_timeout_waits_for_lock_release(tmp_path: Path) -> None:
    """Connection B waits for Connection A to finish its write lock without raising locked error."""
    db_file = tmp_path / "busy_wait.db"

    # Setup table
    init_conn = create_connection(db_file)
    init_conn.execute("CREATE TABLE counter (val INTEGER);")
    init_conn.execute("INSERT INTO counter VALUES (0);")
    init_conn.close()

    conn_a = create_connection(db_file, timeout_seconds=5.0)

    holder_started = threading.Event()
    b_finished = threading.Event()
    b_results: dict[str, Any] = {}

    def _worker_b() -> None:
        holder_started.wait()
        # Brief pause to ensure Conn A is definitely inside transaction
        time.sleep(0.05)
        conn_b = create_connection(db_file, timeout_seconds=5.0)
        try:
            with transaction(conn_b, mode="IMMEDIATE"):
                conn_b.execute("UPDATE counter SET val = val + 10;")
            b_results["success"] = True
        except Exception as exc:
            b_results["error"] = exc
        finally:
            conn_b.close()
            b_finished.set()

    t_b = threading.Thread(target=_worker_b)
    t_b.start()

    try:
        with transaction(conn_a, mode="IMMEDIATE"):
            holder_started.set()
            conn_a.execute("UPDATE counter SET val = val + 1;")
            # Hold lock for 0.2s while thread B is waiting
            time.sleep(0.2)

        # Wait for thread B to complete
        assert b_finished.wait(timeout=5.0)
        t_b.join()

        assert b_results.get("success") is True
        assert "error" not in b_results

        # Verify final value is 11 (0 + 1 from A + 10 from B)
        check_conn = create_connection(db_file)
        try:
            val = check_conn.execute("SELECT val FROM counter;").fetchone()[0]
            assert val == 11
        finally:
            check_conn.close()
    finally:
        conn_a.close()


def test_busy_timeout_expires_when_lock_held_too_long(tmp_path: Path) -> None:
    """Connection B raises OperationalError when busy timeout expires before lock is released."""
    db_file = tmp_path / "busy_timeout_expires.db"

    init_conn = create_connection(db_file)
    init_conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY);")
    init_conn.close()

    # Connection B has very short busy timeout (50ms)
    conn_a = create_connection(db_file, timeout_seconds=5.0)
    conn_b = create_connection(db_file, timeout_seconds=0.05)

    try:
        with transaction(conn_a, mode="IMMEDIATE"):
            conn_a.execute("INSERT INTO items VALUES (1);")

            # Conn B attempts immediate write while lock is held
            with (
                pytest.raises(sqlite3.OperationalError, match="database is locked"),
                transaction(conn_b, mode="IMMEDIATE"),
            ):
                conn_b.execute("INSERT INTO items VALUES (2);")
    finally:
        conn_a.close()
        conn_b.close()


def test_high_contention_with_exponential_backoff_retry(tmp_path: Path) -> None:
    """Multiple threads under high write contention succeed using backoff retries."""
    db_file = tmp_path / "contention_retry.db"

    init_conn = create_connection(db_file)
    init_conn.execute("CREATE TABLE tally (worker_id INTEGER, value INTEGER);")
    init_conn.close()

    thread_count = 6
    increments_per_thread = 10
    errors: list[Exception] = []

    def _worker(worker_id: int) -> None:
        conn = create_connection(db_file, timeout_seconds=0.2)
        try:
            for i in range(increments_per_thread):
                max_retries = 10
                base_delay = 0.01
                for attempt in range(max_retries):
                    try:
                        with transaction(conn, mode="IMMEDIATE"):
                            conn.execute(
                                "INSERT INTO tally (worker_id, value) VALUES (?, ?);",
                                (worker_id, i),
                            )
                        break
                    except sqlite3.OperationalError as exc:
                        if "locked" in str(exc).lower() and attempt < max_retries - 1:
                            time.sleep(base_delay * (1.5**attempt))
                        else:
                            raise
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    assert not errors, f"Encountered errors under contention: {errors}"

    verify_conn = create_connection(db_file)
    try:
        total_rows = verify_conn.execute("SELECT COUNT(*) FROM tally;").fetchone()[0]
        assert total_rows == thread_count * increments_per_thread

        integrity = verify_conn.execute("PRAGMA integrity_check;").fetchone()[0]
        assert integrity == "ok"
    finally:
        verify_conn.close()

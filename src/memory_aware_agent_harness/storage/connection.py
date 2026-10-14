"""SQLite connection factory, pragmas configuration, and transaction management."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

VALID_SYNCHRONOUS_MODES: frozenset[str] = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})


@dataclass(frozen=True)
class StorageSettings:
    """Configurable storage settings for SQLite persistence."""

    database_path: str = ":memory:"
    timeout_seconds: float = 5.0
    synchronous: str = "NORMAL"
    secure_delete: bool = True

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0.0:
            raise ValueError(
                f"StorageSettings.timeout_seconds must be > 0.0, got {self.timeout_seconds}"
            )
        sync_upper = self.synchronous.upper()
        if sync_upper not in VALID_SYNCHRONOUS_MODES:
            raise ValueError(
                f"StorageSettings.synchronous must be one of {sorted(VALID_SYNCHRONOUS_MODES)}, "
                f"got {self.synchronous!r}"
            )
        object.__setattr__(self, "synchronous", sync_upper)


def create_connection(
    database_path: str | Path = ":memory:",
    *,
    timeout_seconds: float = 5.0,
    synchronous: str = "NORMAL",
    secure_delete: bool = True,
) -> sqlite3.Connection:
    """Create a configured SQLite connection with WAL mode and safety PRAGMAs.

    Args:
        database_path: File path or ':memory:' for SQLite database.
        timeout_seconds: Busy timeout in seconds for lock contention.
        synchronous: Synchronous PRAGMA mode ('OFF', 'NORMAL', 'FULL', 'EXTRA').
        secure_delete: Whether to zero-out deleted pages (PRAGMA secure_delete).

    Returns:
        Configured sqlite3.Connection instance with row_factory=sqlite3.Row.
    """
    settings = StorageSettings(
        database_path=str(database_path),
        timeout_seconds=timeout_seconds,
        synchronous=synchronous,
        secure_delete=secure_delete,
    )

    path_str = str(settings.database_path)
    if path_str != ":memory:":
        parent = Path(path_str).parent
        parent.mkdir(parents=True, exist_ok=True)

    # Use isolation_level=None for explicit transaction control (BEGIN IMMEDIATE / COMMIT)
    conn = sqlite3.connect(
        path_str,
        timeout=settings.timeout_seconds,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row

    # Apply safety and performance PRAGMAs
    if path_str != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")

    conn.execute(f"PRAGMA synchronous = {settings.synchronous};")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(f"PRAGMA secure_delete = {'ON' if settings.secure_delete else 'OFF'};")
    conn.execute(f"PRAGMA busy_timeout = {int(settings.timeout_seconds * 1000)};")

    return conn


@contextmanager
def transaction(
    conn: sqlite3.Connection,
    mode: str = "IMMEDIATE",
) -> Iterator[sqlite3.Connection]:
    """Execute a block within an explicit SQLite transaction or savepoint.

    If an outer transaction is already active, uses a nested SAVEPOINT.
    Otherwise, executes 'BEGIN <mode>;' (defaulting to IMMEDIATE to prevent deadlocks).

    Args:
        conn: Target SQLite connection.
        mode: Transaction lock mode ('DEFERRED', 'IMMEDIATE', 'EXCLUSIVE').
    """
    if conn.in_transaction:
        sp_name = f"sp_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {sp_name};")
        try:
            yield conn
            conn.execute(f"RELEASE SAVEPOINT {sp_name};")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
            conn.execute(f"RELEASE SAVEPOINT {sp_name};")
            raise
    else:
        conn.execute(f"BEGIN {mode};")
        try:
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

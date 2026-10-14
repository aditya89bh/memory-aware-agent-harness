"""Versioned SQLite schema migration runner with checksum validation and rollback."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from memory_aware_agent_harness.contracts import FailureDetails, StorageError
from memory_aware_agent_harness.storage.connection import transaction
from memory_aware_agent_harness.storage.schema import SCHEMA_V1_DDL


class MigrationError(StorageError):
    """Raised when a schema migration fails or checksum verification fails."""

    def __init__(
        self,
        message: str,
        *,
        version: int | None = None,
        details: Mapping[str, Any] | FailureDetails | None = None,
        recoverable: bool = False,
    ) -> None:
        d: dict[str, Any] = {}
        if isinstance(details, FailureDetails):
            d.update(details.to_dict())
        elif isinstance(details, Mapping):
            d.update(details)
        if version is not None:
            d["migration_version"] = version
        super().__init__(
            message,
            operation="schema_migration",
            details=d,
            recoverable=recoverable,
        )
        self.error_code = "MIGRATION_ERROR"
        self.version = version


@dataclass(frozen=True)
class Migration:
    """Immutable migration specification."""

    version: int
    description: str
    up_sql: str
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        clean_sql = self.up_sql.strip()
        h = hashlib.sha256(clean_sql.encode("utf-8")).hexdigest()
        object.__setattr__(self, "checksum", h)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="Initial unified SQLite schema with 8 core entities and namespace scoping",
        up_sql=SCHEMA_V1_DDL,
    ),
)


def _split_sql_statements(sql: str) -> list[str]:
    """Split raw SQL text into discrete statements for transactional execution."""
    statements: list[str] = []
    current: list[str] = []
    for line in sql.splitlines():
        clean_line = line.strip()
        if clean_line.startswith("--") or not clean_line:
            continue
        current.append(line)
        if clean_line.endswith(";"):
            statements.append("\n".join(current).strip())
            current = []
    return statements


def get_current_schema_version(conn: sqlite3.Connection) -> int:
    """Return highest applied schema migration version, or 0 if uninitialized."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations';"
    ).fetchone()
    if not row:
        return 0
    version_row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations;"
    ).fetchone()
    return int(version_row[0]) if version_row else 0


def apply_migrations(
    conn: sqlite3.Connection,
    target_version: int | None = None,
) -> int:
    """Apply unapplied migrations up to target_version within an atomic transaction.

    Verifies checksums of previously applied migrations to detect tampering or corruption.

    Args:
        conn: Target SQLite connection.
        target_version: Optional upper bound version. Defaults to latest available.

    Returns:
        The highest applied schema version.
    """
    with transaction(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                checksum TEXT NOT NULL,
                description TEXT NOT NULL
            );
            """
        )

        rows = conn.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version ASC;"
        ).fetchall()
        applied_map = {int(r[0]): str(r[1]) for r in rows}

        latest_available = max((m.version for m in MIGRATIONS), default=0)
        target = target_version if target_version is not None else latest_available

        for migration in MIGRATIONS:
            if migration.version in applied_map:
                recorded_checksum = applied_map[migration.version]
                if recorded_checksum != migration.checksum:
                    raise MigrationError(
                        f"Checksum mismatch for migration version {migration.version}: "
                        f"recorded {recorded_checksum}, expected {migration.checksum}",
                        version=migration.version,
                    )
                continue

            if migration.version > target:
                break

            # Execute migration statements atomically
            for stmt in _split_sql_statements(migration.up_sql):
                try:
                    conn.execute(stmt)
                except sqlite3.Error as exc:
                    raise MigrationError(
                        f"Failed executing migration {migration.version} statement: {exc}",
                        version=migration.version,
                    ) from exc

            applied_at = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT INTO schema_migrations (version, applied_at, checksum, description)
                VALUES (?, ?, ?, ?);
                """,
                (migration.version, applied_at, migration.checksum, migration.description),
            )

    return get_current_schema_version(conn)

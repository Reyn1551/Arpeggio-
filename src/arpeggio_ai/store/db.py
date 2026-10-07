"""SQLite connection, explicit transactions and numbered schema migrations."""

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from arpeggio_ai.core.clock import utc_now
from arpeggio_ai.core.errors import StoreError

log = logging.getLogger(__name__)

BUSY_TIMEOUT_S = 5.0
_MIGRATION_FILE = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def check_sequence(migrations: list[Migration]) -> list[Migration]:
    """Sort migrations and require versions 1..N with no gaps or duplicates."""
    ordered = sorted(migrations, key=lambda migration: migration.version)
    versions = [migration.version for migration in ordered]
    if versions != list(range(1, len(ordered) + 1)):
        raise StoreError(f"migrations must be numbered 1..N without gaps, found {versions}")
    return ordered


def packaged_migrations() -> list[Migration]:
    """The ``NNNN_name.sql`` files shipped in ``arpeggio_ai/store/migrations``."""
    found = []
    for entry in files("arpeggio_ai.store").joinpath("migrations").iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _MIGRATION_FILE.fullmatch(entry.name)
        if match is None:
            raise StoreError(f"migration file name must look like 0001_name.sql: {entry.name}")
        found.append(Migration(int(match[1]), entry.name, entry.read_text(encoding="utf-8")))
    return check_sequence(found)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one write transaction. Commits on success, rolls back on any error.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so reads inside the block (for
    example ``MAX(seq)``) cannot race another writer. Transactions do not nest.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def schema_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration, or 0 for an empty database."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if table is None:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations, each in its own transaction. Returns the versions applied."""
    pending = packaged_migrations() if migrations is None else check_sequence(migrations)
    latest = pending[-1].version if pending else 0
    current = schema_version(conn)
    if current > latest:
        raise StoreError(
            f"database schema version {current} is newer than this Arpeggio supports ({latest})"
        )
    if current == latest:
        return []

    applied = []
    for migration in pending:
        try:
            with transaction(conn):
                # Checked inside the write lock: another process may have just migrated.
                if schema_version(conn) >= migration.version:
                    continue
                conn.executescript(migration.sql)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (migration.version, utc_now()),
                )
        except sqlite3.Error as error:
            raise StoreError(f"migration {migration.name} failed: {error}") from error
        applied.append(migration.version)

    if applied:
        log.info("store.migrated", extra={"from_version": current, "to_version": applied[-1]})
    return applied


def open_db(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database at ``path`` and bring its schema up to date.

    The parent directory must exist. Foreign keys are enforced on this connection and the
    database uses WAL journaling.
    """
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, autocommit=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn

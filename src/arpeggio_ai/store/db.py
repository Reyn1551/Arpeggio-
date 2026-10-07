"""SQLite connection, explicit transactions and numbered schema migrations."""

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from arpeggio_ai.core.clock import utc_now
from arpeggio_ai.core.errors import StoreError

log = logging.getLogger(__name__)

BUSY_TIMEOUT_S = 5.0
BACKUP_DIRNAME = "backups"
MAX_BACKUPS = 5
DIR_MODE = 0o700
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


def _backup_stamp() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"


def _database_file(conn: sqlite3.Connection) -> Path | None:
    """The file behind the ``main`` database, or None for an in-memory database."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def _backup_pattern(db_name: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(db_name)}\.pre-v(\d+)\.(\d{{8}}T\d{{6}}Z)")


def _rotate_backups(backup_dir: Path, db_name: str) -> None:
    """Keep the MAX_BACKUPS newest backups. Only files matching the backup name are touched."""
    pattern = _backup_pattern(db_name)
    found = []
    for entry in backup_dir.iterdir():
        match = pattern.fullmatch(entry.name)
        if match is not None and entry.is_file():
            found.append((match[2], int(match[1]), entry))
    found.sort(reverse=True)
    for _, _, entry in found[MAX_BACKUPS:]:
        entry.unlink()


def backup_database(conn: sqlite3.Connection, from_version: int, to_version: int) -> Path | None:
    """Copy the database to ``backups/<name>.pre-v<to>.<UTC stamp>`` before migrating.

    Uses SQLite's online backup API, which is safe with WAL, writes to a temporary name and
    renames it when complete. Any failure raises StoreError, so the caller never migrates
    without a backup. In-memory databases are not backed up.
    """
    db_file = _database_file(conn)
    if db_file is None:
        return None
    backup_dir = db_file.parent / BACKUP_DIRNAME
    target = backup_dir / f"{db_file.name}.pre-v{to_version}.{_backup_stamp()}"
    partial = target.with_name(target.name + ".tmp")
    try:
        backup_dir.mkdir(mode=DIR_MODE, exist_ok=True)
        if target.exists():
            raise StoreError(f"backup {target} already exists. Wait a second and retry.")
        partial.unlink(missing_ok=True)  # our own leftover from a crash, never a real backup
        dest = sqlite3.connect(partial)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        partial.rename(target)
    except (OSError, sqlite3.Error, StoreError) as error:
        with suppress(OSError):
            partial.unlink(missing_ok=True)
        if isinstance(error, StoreError):
            raise
        raise StoreError(
            f"could not back up the database before migrating to v{to_version}: {error}"
        ) from error

    try:
        _rotate_backups(backup_dir, db_file.name)
    except OSError as error:
        # The backup exists, so migrating is still safe. Old backups just stay around.
        log.warning("store.backup_rotation_failed", extra={"error": str(error)})
    log.info(
        "store.backup_created",
        extra={"path": str(target), "from_version": from_version, "to_version": to_version},
    )
    return target


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations, each in its own transaction. Returns the versions applied.

    A database that already has a schema is backed up first (see ``backup_database``).
    """
    pending = packaged_migrations() if migrations is None else check_sequence(migrations)
    latest = pending[-1].version if pending else 0
    current = schema_version(conn)
    if current > latest:
        raise StoreError(
            f"database schema version {current} is newer than this Arpeggio supports ({latest})"
        )
    if current == latest:
        return []
    if current >= 1:
        backup_database(conn, from_version=current, to_version=latest)

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

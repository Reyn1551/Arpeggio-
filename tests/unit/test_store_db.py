import re
import sqlite3
from pathlib import Path

import pytest

from arpeggio_ai.core.errors import StoreError
from arpeggio_ai.store.db import (
    Migration,
    check_sequence,
    migrate,
    open_db,
    packaged_migrations,
    schema_version,
    transaction,
)

DOCS_SCHEMA = Path(__file__).parents[2] / "docs" / "04-DATA-MODEL.md"


def names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
    )
    return {row[0] for row in rows}


def test_new_database_has_schema_v1(db: sqlite3.Connection) -> None:
    assert schema_version(db) == 1
    assert names(db, "table") == {
        "repos",
        "tasks",
        "done_criteria",
        "attempts",
        "steps",
        "verdicts",
        "approvals",
        "feedback",
        "proposals",
        "eval_runs",
        "eval_results",
        "schema_version",
    }


def test_schema_matches_the_data_model_doc(db: sqlite3.Connection) -> None:
    doc = DOCS_SCHEMA.read_text(encoding="utf-8")
    assert set(re.findall(r"CREATE TABLE (\w+)", doc)) == names(db, "table")
    assert set(re.findall(r"CREATE INDEX (\w+)", doc)) == names(db, "index")


def test_foreign_keys_are_enforced(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO tasks (id, repo_id, title, request, status, created_at)"
            " VALUES ('t1', 'no-such-repo', 'x', 'x', 'intake', 'now')"
        )


def test_wal_journal_mode(db: sqlite3.Connection) -> None:
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_reopening_applies_nothing(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    open_db(path).close()
    conn = open_db(path)
    try:
        assert migrate(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_applies_only_pending_migrations(db: sqlite3.Connection) -> None:
    extra = Migration(2, "0002_extra.sql", "CREATE TABLE extra (id TEXT PRIMARY KEY);")
    assert migrate(db, [*packaged_migrations(), extra]) == [2]
    assert schema_version(db) == 2
    assert "extra" in names(db, "table")


def test_failed_migration_rolls_back_completely(db: sqlite3.Connection) -> None:
    broken = Migration(2, "0002_broken.sql", "CREATE TABLE half (id TEXT);\nCREATE TABLE (oops;")
    with pytest.raises(StoreError, match=r"0002_broken\.sql failed"):
        migrate(db, [*packaged_migrations(), broken])
    assert "half" not in names(db, "table")
    assert schema_version(db) == 1
    assert not db.in_transaction


def test_newer_database_is_rejected(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO schema_version (version, applied_at) VALUES (99, 'x')")
    with pytest.raises(StoreError, match="schema version 99 is newer"):
        migrate(db)


def test_open_db_closes_connection_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "newer.db"
    conn = open_db(path)
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (99, 'x')")
    conn.close()
    with pytest.raises(StoreError):
        open_db(path)
    path.unlink()  # fails on Windows if open_db leaked the connection


@pytest.mark.parametrize("versions", [[1, 3], [1, 1], [2], [0, 1]])
def test_migration_numbers_must_be_contiguous(versions: list[int]) -> None:
    with pytest.raises(StoreError, match="without gaps"):
        check_sequence([Migration(v, f"{v:04d}_x.sql", "") for v in versions])


def test_packaged_migrations_are_in_order() -> None:
    migrations = packaged_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "0001_initial.sql"


def test_transaction_commits_and_rolls_back(db: sqlite3.Connection) -> None:
    with transaction(db):
        db.execute("INSERT INTO repos (id, path, name, created_at) VALUES ('r1', '/a', 'a', 'x')")
    with pytest.raises(RuntimeError), transaction(db):
        db.execute("INSERT INTO repos (id, path, name, created_at) VALUES ('r2', '/b', 'b', 'x')")
        raise RuntimeError("abort")
    assert [row[0] for row in db.execute("SELECT id FROM repos")] == ["r1"]
    assert not db.in_transaction


def test_schema_version_of_empty_database(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "empty.db")
    try:
        assert schema_version(conn) == 0
    finally:
        conn.close()

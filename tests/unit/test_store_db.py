import sqlite3
from pathlib import Path
from typing import Any

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
LATEST = 2


def names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
    )
    return {row[0] for row in rows}


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def doc_schema_sql(doc: str) -> str:
    """The SQL block under the '## Schema' heading of docs/04."""
    section = doc.split("\n## Schema\n", 1)[1]
    return section.split("```sql", 1)[1].split("```", 1)[0]


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every table's columns (order, name, type, not-null, default, pk) and every index."""
    tables = {
        table: [tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")]
        for table in sorted(names(conn, "table"))
    }
    indexes = {
        index: (
            table,
            [row[2] for row in conn.execute(f"PRAGMA index_info({index})")],
        )
        for index, table in conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    return {"tables": tables, "indexes": indexes}


def doc_snapshot(doc: str) -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(doc_schema_sql(doc))
        return snapshot(conn)
    finally:
        conn.close()


def test_new_database_has_the_latest_schema(db: sqlite3.Connection) -> None:
    assert schema_version(db) == LATEST
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
        "quota_usage",
        "schema_version",
    }


def test_schema_matches_the_data_model_doc(db: sqlite3.Connection) -> None:
    documented = doc_snapshot(DOCS_SCHEMA.read_text(encoding="utf-8"))
    actual = snapshot(db)
    assert documented["indexes"] == actual["indexes"]
    assert list(documented["tables"]) == list(actual["tables"])
    for table, cols in actual["tables"].items():
        assert documented["tables"][table] == cols, f"docs/04 differs from the schema in {table}"


def test_doc_check_catches_a_missing_column(db: sqlite3.Connection) -> None:
    doc = DOCS_SCHEMA.read_text(encoding="utf-8")
    lines = [line for line in doc.splitlines() if "deferred_until  TEXT" not in line]
    assert len(lines) == len(doc.splitlines()) - 1
    assert (
        doc_snapshot("\n".join(lines))["tables"]["attempts"] != snapshot(db)["tables"]["attempts"]
    )


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
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == LATEST
    finally:
        conn.close()


def test_applies_only_pending_migrations(db: sqlite3.Connection) -> None:
    extra = Migration(LATEST + 1, "0003_extra.sql", "CREATE TABLE extra (id TEXT PRIMARY KEY);")
    assert migrate(db, [*packaged_migrations(), extra]) == [LATEST + 1]
    assert schema_version(db) == LATEST + 1
    assert "extra" in names(db, "table")


def test_failed_migration_rolls_back_completely(db: sqlite3.Connection) -> None:
    broken = Migration(
        LATEST + 1, "0003_broken.sql", "CREATE TABLE half (id TEXT);\nCREATE TABLE (oops;"
    )
    with pytest.raises(StoreError, match=r"0003_broken\.sql failed"):
        migrate(db, [*packaged_migrations(), broken])
    assert "half" not in names(db, "table")
    assert schema_version(db) == LATEST
    assert not db.in_transaction


# Migration 0002 (budget profiles)


def v1_database(path: Path) -> sqlite3.Connection:
    """A database at schema v1 with one row in every table that 0002 changes."""
    conn = sqlite3.connect(path, autocommit=True)
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn, packaged_migrations()[:1])
    conn.executescript(
        """
        INSERT INTO repos (id, path, name, created_at) VALUES ('R1', '/r', 'r', 'x');
        INSERT INTO tasks (id, repo_id, title, request, status, created_at)
            VALUES ('T1', 'R1', 't', 't', 'merged', 'x');
        INSERT INTO attempts (id, task_id, seq, adapter, model, effort, verification,
            route_reason, status, started_at)
            VALUES ('A1', 'T1', 1, 'api', 'tier1.cheap', 'low', 'light', '{}', 'completed', 'x');
        INSERT INTO steps (id, attempt_id, seq, kind, created_at)
            VALUES ('S1', 'A1', 1, 'model_call', 'x');
        INSERT INTO eval_runs (id, strategy, split, git_sha, config_hash, started_at)
            VALUES ('E1', 'middle', 'holdout', 'abc', 'h', 'x');
        INSERT INTO eval_results (eval_run_id, eval_task, task_id, solved, cost_usd, attempts,
            duration_s) VALUES ('E1', 'web-001', 'T1', 1, 0.5, 1, 12.0);
        """
    )
    return conn


def test_0002_applies_to_an_empty_database(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "new.db", autocommit=True)
    try:
        assert migrate(conn) == [1, 2]
        assert columns(conn, "tasks")[-2:] == ["profile", "deferrable"]
        assert "quota_usage" in names(conn, "table")
    finally:
        conn.close()


def test_0002_keeps_v1_rows_and_fills_defaults(tmp_path: Path) -> None:
    conn = v1_database(tmp_path / "v1.db")
    try:
        assert migrate(conn) == [2]
        row = conn.execute(
            'SELECT profile, "deferrable", status FROM tasks WHERE id = ?', ("T1",)
        ).fetchone()
        assert row == ("unknown", 0, "merged")
        assert conn.execute(
            "SELECT model_mismatch, deferred_until FROM attempts WHERE id = 'A1'"
        ).fetchone() == (0, None)
        assert conn.execute(
            "SELECT actual_model, price_window, price_multiplier, price_cache_hit_per_m"
            " FROM steps WHERE id = 'S1'"
        ).fetchone() == (None, None, 1.0, None)
        assert conn.execute("SELECT profile FROM eval_runs").fetchone() == ("unknown",)
        assert conn.execute("SELECT quota_wait_s FROM eval_results").fetchone() == (0.0,)
        assert conn.execute("SELECT COUNT(*) FROM quota_usage").fetchone() == (0,)
    finally:
        conn.close()


def test_failure_inside_0002_rolls_back_to_v1(tmp_path: Path) -> None:
    conn = v1_database(tmp_path / "v1.db")
    real = packaged_migrations()[1]
    broken = Migration(2, real.name, real.sql + "\nCREATE TABLE (oops;")
    try:
        with pytest.raises(StoreError, match=r"0002_budget_profiles\.sql failed"):
            migrate(conn, [packaged_migrations()[0], broken])
        assert schema_version(conn) == 1
        assert "profile" not in columns(conn, "tasks")
        assert "model_mismatch" not in columns(conn, "attempts")
        assert "quota_usage" not in names(conn, "table")
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone() == (1,)
    finally:
        conn.close()


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

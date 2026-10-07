import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arpeggio_ai.core.errors import StoreError
from arpeggio_ai.core.logs import close_logging, configure_logging
from arpeggio_ai.store import db as db_module
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
LATEST = 5


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
        assert migrate(conn) == list(range(1, LATEST + 1))
        assert columns(conn, "tasks")[-2:] == ["profile", "is_deferrable"]
        assert "quota_usage" in names(conn, "table")
    finally:
        conn.close()


def test_0002_keeps_v1_rows_and_fills_defaults(tmp_path: Path) -> None:
    conn = v1_database(tmp_path / "v1.db")
    try:
        assert migrate(conn) == list(range(2, LATEST + 1))
        row = conn.execute(
            "SELECT profile, is_deferrable, status FROM tasks WHERE id = ?", ("T1",)
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
    backups = tmp_path / "backups"
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
        assert [p.name.split(".")[2] for p in backups.iterdir()] == ["pre-v2"]
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


# Backup before migrating


STAMP = "20261007T120000Z"


@pytest.fixture
def fixed_stamp(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(db_module, "_backup_stamp", lambda: STAMP)
    return STAMP


def open_plain(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, autocommit=True)


def test_backup_is_a_valid_v1_copy_taken_before_migrating(tmp_path: Path, fixed_stamp: str) -> None:
    conn = v1_database(tmp_path / "arpeggio.db")
    try:
        assert migrate(conn) == list(range(2, LATEST + 1))
    finally:
        conn.close()

    backup = tmp_path / "backups" / f"arpeggio.db.pre-v{LATEST}.{STAMP}"
    assert [p.name for p in (tmp_path / "backups").iterdir()] == [backup.name]
    copy = open_plain(backup)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert schema_version(copy) == 1
        assert "profile" not in columns(copy, "tasks")
        assert copy.execute("SELECT id FROM tasks").fetchall() == [("T1",)]
    finally:
        copy.close()


def test_no_backup_for_a_new_database(tmp_path: Path) -> None:
    open_db(tmp_path / "arpeggio.db").close()
    assert not (tmp_path / "backups").exists()


def test_no_backup_when_nothing_is_pending(tmp_path: Path, fixed_stamp: str) -> None:
    open_db(tmp_path / "arpeggio.db").close()
    open_db(tmp_path / "arpeggio.db").close()
    assert not (tmp_path / "backups").exists()


def test_in_memory_database_is_not_backed_up(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:", autocommit=True)
    try:
        migrate(conn, packaged_migrations()[:1])
        assert migrate(conn) == list(range(2, LATEST + 1))
    finally:
        conn.close()
    assert not (tmp_path / "backups").exists()


def test_rotation_keeps_the_five_newest_and_ignores_other_files(
    tmp_path: Path, fixed_stamp: str
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    old = [f"arpeggio.db.pre-v{1 + i % 2}.2026010{i}T000000Z" for i in range(1, 7)]
    others = [
        "notes.txt",
        "arpeggio.db.pre-v2.20250101T000000Z.tmp",
        "other.db.pre-v1.20200101T000000Z",
        "arpeggio.db.pre-vX.20200101T000000Z",
        "arpeggio.db.pre-v1.2020",
    ]
    for name in old + others:
        (backups / name).write_text("x", encoding="utf-8")
    conn = v1_database(tmp_path / "arpeggio.db")
    try:
        migrate(conn)
    finally:
        conn.close()

    remaining = sorted(p.name for p in backups.iterdir())
    kept = sorted([f"arpeggio.db.pre-v{LATEST}.{STAMP}", *old[2:]])
    assert remaining == sorted(kept + others)


def test_failed_backup_aborts_the_migration(tmp_path: Path) -> None:
    (tmp_path / "backups").write_text("a file where the backup directory should be")
    conn = v1_database(tmp_path / "arpeggio.db")
    try:
        with pytest.raises(StoreError, match="could not back up the database before migrating"):
            migrate(conn)
        assert schema_version(conn) == 1
        assert "profile" not in columns(conn, "tasks")
    finally:
        conn.close()


def test_existing_backup_is_never_overwritten(tmp_path: Path, fixed_stamp: str) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    existing = backups / f"arpeggio.db.pre-v{LATEST}.{STAMP}"
    existing.write_text("older backup", encoding="utf-8")
    conn = v1_database(tmp_path / "arpeggio.db")
    try:
        with pytest.raises(StoreError, match="already exists"):
            migrate(conn)
        assert schema_version(conn) == 1
    finally:
        conn.close()
    assert existing.read_text(encoding="utf-8") == "older backup"
    assert sorted(p.name for p in backups.iterdir()) == [existing.name]


def test_backup_is_logged(tmp_path: Path, fixed_stamp: str) -> None:
    log_file = configure_logging(tmp_path / "logs")
    conn = v1_database(tmp_path / "arpeggio.db")
    try:
        migrate(conn)
    finally:
        conn.close()
    close_logging()
    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    backup = next(e for e in entries if e["event"] == "store.backup_created")
    assert backup["path"] == str(tmp_path / "backups" / f"arpeggio.db.pre-v{LATEST}.{STAMP}")
    assert (backup["from_version"], backup["to_version"]) == (1, LATEST)
    assert [e["event"] for e in entries] == [
        "store.migrated",
        "store.backup_created",
        "store.migrated",
    ]


def test_stale_partial_backup_is_replaced(tmp_path: Path, fixed_stamp: str) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / f"arpeggio.db.pre-v{LATEST}.{STAMP}.tmp").write_text("left by a crash")
    conn = v1_database(tmp_path / "arpeggio.db")
    try:
        assert migrate(conn) == list(range(2, LATEST + 1))
    finally:
        conn.close()
    assert [p.name for p in backups.iterdir()] == [f"arpeggio.db.pre-v{LATEST}.{STAMP}"]


def v2_database(path: Path) -> sqlite3.Connection:
    """A database at schema v2 (before the rename) with a deferrable task."""
    conn = v1_database(path)
    migrate(conn, packaged_migrations()[:2])
    conn.execute(
        'INSERT INTO tasks (id, repo_id, title, request, status, created_at, profile, "deferrable")'
        " VALUES ('T2', 'R1', 'eval', 'eval', 'intake', 'x', 'free', 1)"
    )
    return conn


def test_v2_database_with_rows_upgrades_to_v3_after_a_backup(
    tmp_path: Path, fixed_stamp: str
) -> None:
    conn = v2_database(tmp_path / "arpeggio.db")
    try:
        assert schema_version(conn) == 2
        assert migrate(conn, packaged_migrations()[:3]) == [3]
        assert "deferrable" not in columns(conn, "tasks")
        rows = conn.execute("SELECT id, profile, is_deferrable FROM tasks ORDER BY id").fetchall()
        assert [tuple(row) for row in rows] == [("T1", "unknown", 0), ("T2", "free", 1)]
    finally:
        conn.close()

    # pre-v2 comes from building the v2 fixture, pre-v3 from this upgrade.
    names = sorted(b.name for b in (tmp_path / "backups").iterdir())
    assert names == [f"arpeggio.db.pre-v2.{STAMP}", f"arpeggio.db.pre-v3.{STAMP}"]
    copy = open_plain(tmp_path / "backups" / f"arpeggio.db.pre-v3.{STAMP}")
    try:
        assert schema_version(copy) == 2
        assert columns(copy, "tasks")[-1] == "deferrable"
    finally:
        copy.close()


def test_v3_database_with_steps_upgrades_to_v4(tmp_path: Path) -> None:
    conn = v1_database(tmp_path / "arpeggio.db")
    try:
        migrate(conn, packaged_migrations()[:3])
        assert migrate(conn, packaged_migrations()[:4]) == [4]
        assert columns(conn, "steps")[-2:] == ["provider", "prompt_overhead_tokens"]
        row = conn.execute(
            "SELECT provider, prompt_overhead_tokens FROM steps WHERE id = 'S1'"
        ).fetchone()
        assert tuple(row) == (None, None)
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(steps)")}
        assert "idx_steps_provider" in indexes
    finally:
        conn.close()


def test_v4_database_with_attempts_upgrades_to_v5(tmp_path: Path) -> None:
    conn = v1_database(tmp_path / "arpeggio.db")
    try:
        migrate(conn, packaged_migrations()[:4])
        assert migrate(conn) == [5]
        assert columns(conn, "attempts")[-2:] == ["base_sha", "failure_reason"]
        row = conn.execute(
            "SELECT model, status, base_sha, failure_reason FROM attempts WHERE id = 'A1'"
        ).fetchone()
        assert tuple(row) == ("tier1.cheap", "completed", None, None)
    finally:
        conn.close()

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from arpeggio_ai.core.logs import REDACTED, close_logging, configure_logging, log_context
from arpeggio_ai.paths import logs_dir
from arpeggio_ai.store.repositories import create_attempt, create_task, ensure_repo

log = logging.getLogger("arpeggio_ai.test")


@pytest.fixture
def log_file(home: Path) -> Path:
    return configure_logging(logs_dir())


def lines(path: Path) -> list[dict[str, Any]]:
    close_logging()  # flush and release the file before reading (Windows)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_log_file_is_named_by_utc_date(log_file: Path, home: Path) -> None:
    assert log_file == home / "logs" / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"


def test_each_line_is_a_json_object(log_file: Path) -> None:
    log.info("first", extra={"n": 1})
    log.warning("second %s", "arg")
    entries = lines(log_file)
    assert [e["event"] for e in entries] == ["first", "second arg"]
    assert [e["level"] for e in entries] == ["info", "warning"]
    assert entries[0]["logger"] == "arpeggio_ai.test"
    assert entries[0]["n"] == 1
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", entries[0]["ts"])
    assert entries[0]["task_id"] is None
    assert entries[0]["attempt_id"] is None


def test_log_lines_correlate_with_database_rows(log_file: Path, db: sqlite3.Connection) -> None:
    repo = ensure_repo(db, "/code/app", "app")
    task = create_task(db, repo.id, "t", "t", profile="free")
    attempt = create_attempt(
        db,
        task.id,
        adapter="api",
        model="tier1.cheap",
        effort="low",
        verification="light",
        route_reason={"rule": "x"},
    )
    with log_context(task_id=task.id, attempt_id=attempt.id):
        log.info("attempt.started", extra={"seq": attempt.seq})

    entries = lines(log_file)
    assert [e["event"] for e in entries] == ["store.migrated", "attempt.started"]
    entry = entries[1]
    row = db.execute(
        "SELECT a.seq FROM attempts a JOIN tasks t ON t.id = a.task_id WHERE t.id = ? AND a.id = ?",
        (entry["task_id"], entry["attempt_id"]),
    ).fetchone()
    assert row is not None
    assert row[0] == entry["seq"] == 1


def test_contexts_nest_and_reset(log_file: Path) -> None:
    with log_context(task_id="T1"):
        log.info("task only")
        with log_context(attempt_id="A1"):
            log.info("both")
        with log_context(task_id="T2", attempt_id="A2"):
            log.info("inner wins")
        log.info("attempt gone")
    log.info("all gone")

    ids = [(e["task_id"], e["attempt_id"]) for e in lines(log_file)]
    assert ids == [("T1", None), ("T1", "A1"), ("T2", "A2"), ("T1", None), (None, None)]


def test_explicit_ids_override_context(log_file: Path) -> None:
    with log_context(task_id="T1"):
        log.info("x", extra={"task_id": "T9"})
    assert lines(log_file)[0]["task_id"] == "T9"


def test_secret_looking_fields_are_redacted(log_file: Path) -> None:
    log.info(
        "call",
        extra={
            "api_key": "sk-test-123",
            "access_token": "abc",
            "password": "hunter2",
            "authorization": "Bearer x",
            "input_tokens": 1200,
            "cached_tokens": 5,
            "provider": "anthropic",
        },
    )
    (entry,) = lines(log_file)
    assert entry["api_key"] == entry["access_token"] == entry["password"] == REDACTED
    assert entry["authorization"] == REDACTED
    assert (entry["input_tokens"], entry["cached_tokens"]) == (1200, 5)
    assert entry["provider"] == "anthropic"
    assert "sk-test-123" not in log_file.read_text(encoding="utf-8")


def test_exceptions_are_summarized(log_file: Path) -> None:
    try:
        raise ValueError("bad thing")
    except ValueError:
        log.exception("failed")
    (entry,) = lines(log_file)
    assert (entry["level"], entry["exc_type"], entry["exc"]) == ("error", "ValueError", "bad thing")


def test_non_json_values_are_stringified(log_file: Path, tmp_path: Path) -> None:
    log.info("path", extra={"where": tmp_path})
    assert lines(log_file)[0]["where"] == str(tmp_path)


def test_debug_is_dropped_at_info_level(log_file: Path) -> None:
    log.debug("noise")
    log.info("signal")
    assert [e["event"] for e in lines(log_file)] == ["signal"]


def test_configure_twice_keeps_one_handler(log_file: Path) -> None:
    configure_logging(log_file.parent)
    log.info("once")
    assert [e["event"] for e in lines(log_file)] == ["once"]


def test_unconfigured_logging_is_silent(capfd: pytest.CaptureFixture[str]) -> None:
    log.warning("nobody listens")
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""

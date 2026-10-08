import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from gitrepo import link_directory, make_repo

from arpeggio_ai.safety import process as process_module
from arpeggio_ai.safety.process import scrubbed_env
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.repositories import (
    Attempt,
    Task,
    Verdict,
    add_criterion,
    create_attempt,
    create_task,
    list_verdicts,
    register_repo,
)
from arpeggio_ai.verify.runner import run_criteria

PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def task(db: sqlite3.Connection, repo: Path) -> Task:
    registered = register_repo(db, repo)
    return create_task(db, registered.id, "Fix add", "fix calc.add", profile="micro")


@pytest.fixture
def attempt(db: sqlite3.Connection, task: Task) -> Attempt:
    return create_attempt(
        db,
        task.id,
        adapter="api",
        model="tier1.flash",
        effort="low",
        verification="light",
        route_reason={},
    )


@pytest.fixture
def artifacts(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


def verify(
    db: sqlite3.Connection,
    artifacts: ArtifactStore,
    task: Task,
    attempt: Attempt,
    repo: Path,
    env: dict[str, str] | None = None,
) -> list[Verdict]:
    return asyncio.run(
        run_criteria(
            db,
            artifacts,
            task_id=task.id,
            attempt_id=attempt.id,
            worktree=repo,
            env=env or scrubbed_env(),
        )
    )


def add(db: sqlite3.Connection, task: Task, **spec: Any) -> None:
    add_criterion(db, task.id, spec)


def test_every_criterion_runs_in_order_and_is_stored(
    db: sqlite3.Connection, artifacts: ArtifactStore, task: Task, attempt: Attempt, repo: Path
) -> None:
    add(db, task, kind="command", argv=[*PYTEST, "tests/test_calc.py"])  # fails: the bug
    add(db, task, kind="command", argv=[*PYTEST, "tests/test_ok.py"])
    add(db, task, kind="file_exists", path="src/calc/ops.py")
    verdicts = verify(db, artifacts, task, attempt, repo)
    assert [v.passed for v in verdicts] == [False, True, True]
    assert list_verdicts(db, attempt.id) == verdicts
    failed = verdicts[0]
    assert failed.kind == "check" and failed.cost_usd == 0.0
    assert failed.detail is not None
    assert (
        failed.detail["exit_code"],
        failed.detail["expect_exit"],
        failed.detail["timed_out"],
    ) == (1, 0, False)
    assert "assert -1 == 5" in failed.detail["output_tail"]
    assert failed.log_ref == f"{task.id}/{attempt.id}/check-1.log"
    assert b"test_add" in artifacts.read(failed.log_ref)
    assert verdicts[1].log_ref == f"{task.id}/{attempt.id}/check-2.log"
    assert verdicts[2].log_ref is None
    assert verdicts[2].detail == {"path": "src/calc/ops.py", "exists": True}


def test_expected_non_zero_exit(
    db: sqlite3.Connection, artifacts: ArtifactStore, task: Task, attempt: Attempt, repo: Path
) -> None:
    add(db, task, kind="command", argv=[sys.executable, "-c", "raise SystemExit(4)"], expect_exit=4)
    assert [v.passed for v in verify(db, artifacts, task, attempt, repo)] == [True]


def test_output_tail_keeps_the_last_2000_characters(
    db: sqlite3.Connection, artifacts: ArtifactStore, task: Task, attempt: Attempt, repo: Path
) -> None:
    add(db, task, kind="command", argv=[sys.executable, "-c", "print('a' * 5000 + 'END')"])
    [verdict] = verify(db, artifacts, task, attempt, repo)
    assert verdict.detail is not None
    tail = verdict.detail["output_tail"]
    assert len(tail) == 2000 and tail.rstrip().endswith("END")


def test_timeout_fails_the_check(
    db: sqlite3.Connection, artifacts: ArtifactStore, task: Task, attempt: Attempt, repo: Path
) -> None:
    add(
        db,
        task,
        kind="command",
        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        timeout_s=1,
    )
    [verdict] = verify(db, artifacts, task, attempt, repo)
    assert verdict.passed is False
    assert verdict.detail is not None
    assert (verdict.detail["timed_out"], verdict.detail["exit_code"]) == (True, None)


def test_missing_executable_fails_the_check(
    db: sqlite3.Connection, artifacts: ArtifactStore, task: Task, attempt: Attempt, repo: Path
) -> None:
    add(db, task, kind="command", argv=["no-such-tool"])
    [verdict] = verify(db, artifacts, task, attempt, repo)
    assert verdict.passed is False
    assert verdict.detail is not None
    assert "executable not found" in verdict.detail["error"]
    assert verdict.log_ref is not None


def test_missing_file(
    db: sqlite3.Connection, artifacts: ArtifactStore, task: Task, attempt: Attempt, repo: Path
) -> None:
    add(db, task, kind="file_exists", path="src/calc/new.py")
    [verdict] = verify(db, artifacts, task, attempt, repo)
    assert (verdict.passed, verdict.detail) == (False, {"path": "src/calc/new.py", "exists": False})


def test_file_reached_through_a_symlink_outside_does_not_count(
    db: sqlite3.Connection,
    artifacts: ArtifactStore,
    task: Task,
    attempt: Attempt,
    repo: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    link_directory(outside, repo / "link")
    add(db, task, kind="file_exists", path="link/secret.txt")
    [verdict] = verify(db, artifacts, task, attempt, repo)
    assert verdict.passed is False
    assert verdict.detail is not None and verdict.detail["error"] == "resolves outside the worktree"


def test_no_criteria_gives_no_verdicts(
    db: sqlite3.Connection, artifacts: ArtifactStore, task: Task, attempt: Attempt, repo: Path
) -> None:
    assert verify(db, artifacts, task, attempt, repo) == []


def test_output_limit_fails_the_check(
    db: sqlite3.Connection,
    artifacts: ArtifactStore,
    task: Task,
    attempt: Attempt,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "MAX_TEMP_OUTPUT_BYTES", 1024 * 1024)
    monkeypatch.setattr(process_module, "POLL_S", 0.1)
    flood = "import sys\nwhile True: sys.stdout.write('y' * 999 + '\\n')"
    add(db, task, kind="command", argv=[sys.executable, "-c", flood], timeout_s=60)
    [verdict] = verify(db, artifacts, task, attempt, repo)
    assert verdict.passed is False
    assert verdict.detail is not None
    assert (verdict.detail["output_limit_exceeded"], verdict.detail["timed_out"]) == (True, False)
    assert verdict.detail["exit_code"] is None

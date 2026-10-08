"""Suite discovery, splits, the holdout share and the latest self-check status (EVL-01, EVL-02)."""

import json
from pathlib import Path

import pytest
import yaml

from arpeggio_ai.evals.suite import TaskEntry, discover, latest_statuses, select, summarize


def write_task(root: Path, split_dir: str, task_id: str, repo: Path) -> None:
    data = {
        "id": task_id,
        "category": "bugfix",
        "expected_risk": "low",
        "repo": {"path": repo.as_posix(), "base": "abc1234", "solution": "abc1235"},
        "request": "Fix the thing that is broken.",
        "done_criteria": [{"kind": "file_exists", "path": "README.md"}],
    }
    folder = root / split_dir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{task_id}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    (path / ".git").mkdir(parents=True)
    return path


def test_discover_both_splits(tmp_path: Path, repo: Path) -> None:
    root = tmp_path / "evals"
    write_task(root, "tasks", "task-b", repo)
    write_task(root, "tasks", "task-a", repo)
    write_task(root, "holdout", "task-c", repo)
    entries = discover(root)
    assert [(e.id, e.split) for e in entries] == [
        ("task-a", "tuning"),
        ("task-b", "tuning"),
        ("task-c", "holdout"),
    ]
    assert all(entry.task is not None for entry in entries)
    assert [e.id for e in select(entries, "holdout")] == ["task-c"]
    assert [e.id for e in select(entries, "all", ["task-b"])] == ["task-b"]


def test_id_in_both_splits_is_invalid(tmp_path: Path, repo: Path) -> None:
    root = tmp_path / "evals"
    write_task(root, "tasks", "same-id", repo)
    write_task(root, "holdout", "same-id", repo)
    entries = discover(root)
    assert all(entry.task is None for entry in entries)
    assert all("exists in both splits" in entry.problems[-1] for entry in entries)


def test_missing_evals_dir_is_an_empty_suite(tmp_path: Path) -> None:
    assert discover(tmp_path / "nothing") == []


def entries(tuning: int, holdout: int) -> list[TaskEntry]:
    made = [TaskEntry(f"t{n:03d}", "tuning", Path()) for n in range(tuning)]
    return made + [TaskEntry(f"h{n:03d}", "holdout", Path()) for n in range(holdout)]


def test_summary_without_warnings() -> None:
    summary = summarize(entries(14, 6))
    assert (summary.tuning, summary.holdout, summary.total) == (14, 6, 20)
    assert summary.holdout_share == pytest.approx(0.3)
    assert summary.warnings == []


def test_summary_warns_on_small_suite_and_low_holdout() -> None:
    summary = summarize(entries(2, 1))
    assert summary.warnings == ["only 3 tasks; EVL-01 asks for at least 20"]
    summary = summarize(entries(17, 4))
    assert summary.warnings == ["holdout share 19% is below the 30% EVL-02 asks for"]
    assert summarize([]).holdout_share == 0.0


def test_latest_status_comes_from_the_newest_report(tmp_path: Path) -> None:
    reports = tmp_path / ".reports"
    reports.mkdir()
    old = {"tasks": [{"id": "a", "status": "unsolvable"}, {"id": "b", "status": "valid"}]}
    new = {"tasks": [{"id": "a", "status": "valid"}]}
    (reports / "check-20261001T000000000000Z.json").write_text(json.dumps(old))
    (reports / "check-20261002T000000000000Z.json").write_text(json.dumps(new))
    (reports / "check-20261003T000000000000Z.json").write_text("not json")
    (reports / "other.json").write_text(json.dumps({"tasks": [{"id": "a", "status": "x"}]}))
    assert latest_statuses(tmp_path) == {"a": "valid", "b": "valid"}
    assert latest_statuses(tmp_path / "none") == {}

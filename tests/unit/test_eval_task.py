"""Eval task schema and loader (EVL-01)."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from gitrepo import link_directory

from arpeggio_ai.evals.task import EvalTask, TaskError, load_task, substitute


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "evals"
    (path / "tasks").mkdir(parents=True)
    (path / "fixtures" / "demo-task").mkdir(parents=True)
    (path / "fixtures" / "demo-task" / "test_x.py").write_text("def test(): pass\n")
    (path / "fixtures" / "demo-task" / "reference.diff").write_text("diff\n")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    (path / ".git").mkdir(parents=True)
    return path


def task_data(repo: Path, **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "demo-task",
        "category": "feature",
        "expected_risk": "low",
        "repo": {"path": repo.as_posix(), "base": "3f2a9c1", "solution": "9b81d07"},
        "request": "Add email validation to the form.",
        "setup": [{"argv": ["uv", "sync"], "timeout_s": 600}],
        "done_criteria": [{"kind": "command", "argv": ["pytest", "tests/test_x.py"]}],
        "hidden_tests": [{"path": "tests/test_x.py", "source": "fixtures/demo-task/test_x.py"}],
        "reference_diff": "fixtures/demo-task/reference.diff",
        "context_files": ["src/app.py"],
        "tags": ["python"],
        "notes": "free text",
    }
    data.update(changes)
    return data


def write(root: Path, data: dict[str, Any], name: str = "demo-task") -> Path:
    file = root / "tasks" / f"{name}.yaml"
    file.write_text(yaml.safe_dump(data), encoding="utf-8")
    return file


def problems(root: Path, data: dict[str, Any], name: str = "demo-task") -> str:
    with pytest.raises(TaskError) as info:
        load_task(write(root, data, name), root)
    return "; ".join(info.value.problems)


def test_valid_task_loads(root: Path, repo: Path) -> None:
    task = load_task(write(root, task_data(repo)), root)
    assert task.id == "demo-task"
    assert task.setup[0].timeout_s == 600
    assert task.criteria[0].kind == "command"


def test_solution_or_reference_diff_is_enough(root: Path, repo: Path) -> None:
    data = task_data(repo, reference_diff=None)
    assert load_task(write(root, data), root).repo.solution == "9b81d07"
    data = task_data(repo)
    del data["repo"]["solution"]
    assert load_task(write(root, data), root).reference_diff is not None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"id": "Bad_ID"}, "id: must match"),
        ({"id": "ab"}, "id: must match"),
        ({"category": "chore"}, "category: "),
        ({"expected_risk": "extreme"}, "expected_risk: "),
        ({"request": "too short"}, "request: String should have at least 10"),
        ({"request": "x" * 4001}, "request: String should have at most 4000"),
        ({"request": "TODO: describe the task"}, "request: still a TODO"),
        ({"done_criteria": []}, "done_criteria: List should have at least 1"),
        ({"done_criteria": [{"kind": "command", "argv": "pytest -q"}]}, "shell strings"),
        ({"setup": [{"argv": "composer install"}]}, "shell strings"),
        ({"setup": [{"argv": ["x"], "timeout_s": 0}]}, "setup.0.timeout_s"),
        ({"unknown": 1}, "unknown: Extra inputs are not permitted"),
        ({"context_files": ["tests/test_x.py"]}, "hidden test paths must not be context_files"),
        ({"reference_diff": "../outside.diff"}, "reference_diff: must not contain"),
        ({"reference_diff": "C:/abs.diff"}, "reference_diff: must be relative"),
        ({"context_files": ["/etc/passwd"]}, "context_files: must be relative"),
    ],
)
def test_validation_rules(root: Path, repo: Path, change: dict[str, Any], expected: str) -> None:
    name = change.get("id", "demo-task")
    found = problems(root, task_data(repo, **change), name)
    assert expected in found


def test_solution_and_reference_diff_both_missing(root: Path, repo: Path) -> None:
    data = task_data(repo, reference_diff=None)
    del data["repo"]["solution"]
    assert "give repo.solution or reference_diff" in problems(root, data)


@pytest.mark.parametrize(
    ("repo_change", "field", "message"),
    [
        ({"base": "HEAD"}, "repo.base", "must be a commit SHA"),
        ({"base": 1234567}, "repo.base", "quote the commit SHA"),
        ({"solution": "xyz"}, "repo.solution", "must be a commit SHA"),
        ({"path": "relative/repo"}, "repo.path", "must be an absolute local path"),
    ],
)
def test_repo_rules(
    root: Path, repo: Path, repo_change: dict[str, Any], field: str, message: str
) -> None:
    data = task_data(repo)
    data["repo"].update(repo_change)
    found = problems(root, data)
    assert field in found and message in found


def test_hidden_paths_must_be_relative_and_unique(root: Path, repo: Path) -> None:
    source = "fixtures/demo-task/test_x.py"
    bad = [{"path": "../x.py", "source": source}]
    assert "hidden_tests.0.path" in problems(root, task_data(repo, hidden_tests=bad))
    twice = [{"path": "tests/a.py", "source": source}] * 2
    assert "repeat a path" in problems(root, task_data(repo, hidden_tests=twice))


def test_referenced_files_must_exist(root: Path, repo: Path) -> None:
    hidden = [{"path": "tests/test_y.py", "source": "fixtures/demo-task/missing.py"}]
    data = task_data(repo, hidden_tests=hidden, reference_diff="fixtures/nope.diff")
    found = problems(root, data)
    assert "hidden_tests.0.source: file not found" in found
    assert "reference_diff: file not found" in found


def test_fixture_symlink_escaping_the_evals_dir_is_rejected(
    root: Path, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "reference.diff").write_text("diff\n")
    link_directory(outside, root / "fixtures" / "escape")
    data = task_data(repo, reference_diff="fixtures/escape/reference.diff")
    assert "reference_diff: resolves outside the evals directory" in problems(root, data)


def test_id_must_equal_file_name(root: Path, repo: Path) -> None:
    assert "must equal the file name 'other-name'" in problems(root, task_data(repo), "other-name")


def test_repo_must_be_a_git_repository(root: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "repo.path: not a git repository" in problems(root, task_data(plain))


@pytest.mark.parametrize("text", ["- a list\n", "a: [unclosed\n", "!!python/object:os.system {}\n"])
def test_unreadable_or_unsafe_yaml(root: Path, text: str) -> None:
    file = root / "tasks" / "demo-task.yaml"
    file.write_text(text, encoding="utf-8")
    with pytest.raises(TaskError):
        load_task(file, root)


def test_placeholders_only_with_variables(root: Path, repo: Path) -> None:
    data = task_data(repo)
    data["repo"]["path"] = "${REPO}"
    file = write(root, data)
    assert load_task(file, root, {"REPO": repo.as_posix()}).repo.path == repo.as_posix()
    with pytest.raises(TaskError, match="must be an absolute"):
        load_task(file, root)
    with pytest.raises(TaskError, match=r"unknown placeholder \$\{REPO\}"):
        load_task(file, root, {})


def test_substitute_walks_nested_data() -> None:
    data = {"a": ["${X}-1", 2, {"b": "${X}"}]}
    assert substitute(data, {"X": "v"}) == {"a": ["v-1", 2, {"b": "v"}]}


def test_model_is_frozen(repo: Path) -> None:
    task = EvalTask.model_validate(task_data(repo))
    with pytest.raises(Exception, match="frozen"):
        task.id = "other"  # type: ignore[misc]

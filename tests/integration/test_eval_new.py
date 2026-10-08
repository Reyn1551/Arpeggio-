"""`eval new` scaffolding from two commits (EVL-01)."""

import asyncio
import json
from pathlib import Path

import pytest
from evalkit import ENV, Suite
from samplerepo import git

from arpeggio_ai.evals.scaffold import NewTask, ScaffoldError, scaffold
from arpeggio_ai.evals.suite import discover
from arpeggio_ai.safety.secret_scan import SecretScanner
from arpeggio_ai.safety.worktree import WorktreeError

FAKE_AWS_KEY = "AKIAQ3EGRYKUZLNVHM7X"


@pytest.fixture
def suite(tmp_path: Path, home: Path) -> Suite:
    s = Suite(tmp_path, home)
    s.root = tmp_path / "my-evals"  # empty personal suite
    return s


def new(suite: Suite, base: str = "c1", solution: str = "c2", **kwargs: object) -> NewTask:
    kwargs.setdefault("task_id", "calc-mul")
    return asyncio.run(
        scaffold(  # type: ignore[arg-type]
            suite.repo,
            suite.sha(base),
            suite.sha(solution),
            evals_root=suite.root,
            home=suite.home,
            env=ENV,
            scanner=SecretScanner(),
            **kwargs,
        )
    )


def test_default_globs_extract_hidden_tests(suite: Suite) -> None:
    result = new(suite)
    assert result.split == "tuning"
    assert result.task_file == suite.root / "tasks" / "calc-mul.yaml"
    assert result.hidden_tests == ["tests/test_mul.py"]
    hidden = suite.root / "fixtures/calc-mul/hidden/tests/test_mul.py"
    assert (
        hidden.read_bytes()
        == git(suite.repo, "show", f"{suite.sha('c2')}:tests/test_mul.py").encode("utf-8") + b"\n"
    )
    assert result.reference_diff == "fixtures/calc-mul/reference.diff"
    diff = (suite.root / result.reference_diff).read_text(encoding="utf-8")
    assert "src/calc/ops.py" in diff and "test_mul" not in diff


def test_custom_globs(suite: Suite) -> None:
    result = new(suite, test_globs=["src/**"])
    assert result.hidden_tests == ["src/calc/ops.py"]
    assert result.reference_diff is not None
    diff = (suite.root / result.reference_diff).read_text(encoding="utf-8")
    assert "tests/test_mul.py" in diff and "src/calc/ops.py" not in diff


def test_only_test_changes_give_no_reference_diff(suite: Suite) -> None:
    result = new(suite, test_globs=["**"])
    assert result.reference_diff is None
    assert "reference_diff: null" in result.task_file.read_text(encoding="utf-8")


def test_scaffolded_task_is_invalid_until_filled_in(suite: Suite) -> None:
    new(suite)
    (entry,) = discover(suite.root)
    assert entry.task is None
    problems = "; ".join(entry.problems)
    assert "request: still a TODO" in problems
    assert "done_criteria: List should have at least 1" in problems


def test_filled_in_task_passes_check(suite: Suite) -> None:
    """The owner flow: eval new, edit request and done_criteria, eval check."""
    result = new(suite)
    text = result.task_file.read_text(encoding="utf-8")
    text = text.replace('request: "TODO: describe the task"', "request: Add mul(a, b) to calc.ops.")
    python = suite.variables["PYTHON"]
    criteria = (
        "done_criteria:\n  - kind: command\n"
        f'    argv: ["{python}", "-m", "unittest", "discover", "-s", "tests", "-p", "test_mul.py"]'
    )
    text = text.replace("done_criteria: []", criteria, 1)
    result.task_file.write_text(text, encoding="utf-8")
    check = suite.check("calc-mul")
    assert check.status == "valid", check.detail


def test_holdout_flag(suite: Suite) -> None:
    result = new(suite, holdout=True)
    assert result.split == "holdout"
    assert result.task_file == suite.root / "holdout" / "calc-mul.yaml"


def test_refuses_overwrite_without_force(suite: Suite) -> None:
    new(suite)
    with pytest.raises(ScaffoldError, match="already exists"):
        new(suite)
    with pytest.raises(ScaffoldError, match="already exists in the tuning split"):
        new(suite, holdout=True, force=True)
    stale = suite.root / "fixtures/calc-mul/stale.txt"
    stale.write_text("old")
    again = new(suite, force=True, test_globs=["src/**"])
    assert again.hidden_tests == ["src/calc/ops.py"]
    assert not stale.exists()


def test_scanner_runs_on_written_files(suite: Suite) -> None:
    (suite.repo / "settings.py").write_text(f'AWS = "{FAKE_AWS_KEY}"\n')
    git(suite.repo, "add", "-A")
    git(suite.repo, "commit", "-q", "-m", "add settings")
    head = git(suite.repo, "rev-parse", "HEAD")
    suite.variables["SAMPLE_C4"] = head
    result = new(suite, base="c3", solution="c4")
    assert result.files_scanned == 2  # reference.diff and the task file
    assert result.findings == {"aws_access_key": 1}
    assert FAKE_AWS_KEY not in json.dumps(result.to_dict())


@pytest.mark.parametrize("task_id", ["Bad", "ab", "-start"])
def test_invalid_id(suite: Suite, task_id: str) -> None:
    with pytest.raises(ScaffoldError, match="invalid task id"):
        new(suite, task_id=task_id)


def test_not_a_repository_root(suite: Suite) -> None:
    with pytest.raises(ScaffoldError, match="not the root of a git repository"):
        asyncio.run(
            scaffold(
                suite.repo / "src",
                suite.sha("c1"),
                suite.sha("c2"),
                "calc-mul",
                suite.root,
                home=suite.home,
                env=ENV,
                scanner=SecretScanner(),
            )
        )


def test_unknown_commit_and_empty_change(suite: Suite) -> None:
    with pytest.raises(WorktreeError, match="not found"):
        asyncio.run(
            scaffold(
                suite.repo,
                "deadbeef",
                suite.sha("c2"),
                "calc-mul",
                suite.root,
                home=suite.home,
                env=ENV,
                scanner=SecretScanner(),
            )
        )
    with pytest.raises(ScaffoldError, match="no changes"):
        new(suite, base="c2", solution="c2")

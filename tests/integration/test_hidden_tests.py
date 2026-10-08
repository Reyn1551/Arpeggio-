"""Hidden tests: injected after the patch and before the checks (EVL-01)."""

import logging
from pathlib import Path

import pytest
from evalkit import Suite

from arpeggio_ai.evals.hidden import HiddenTestError, hidden_paths, inject_hidden_tests
from arpeggio_ai.evals.task import EvalTask


@pytest.fixture
def suite(tmp_path: Path, home: Path) -> Suite:
    return Suite(tmp_path, home)


def task_with_hidden(suite: Suite, source: str) -> EvalTask:
    data = suite.base_task(hidden_tests=[{"path": "tests/test_calc.py", "source": source}])
    return EvalTask.model_validate(suite.resolved(data))


def test_inject_copies_and_logs_overwrites(
    suite: Suite, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = suite.write_fixture("fixtures/custom-task/test_calc.py", b"# hidden\n")
    task = task_with_hidden(suite, source)
    worktree = tmp_path / "wt"
    (worktree / "tests").mkdir(parents=True)
    (worktree / "tests" / "test_calc.py").write_bytes(b"# visible\n")
    with caplog.at_level(logging.INFO):
        overwritten = inject_hidden_tests(worktree, task, suite.root)
    assert overwritten == ["tests/test_calc.py"]
    assert (worktree / "tests" / "test_calc.py").read_bytes() == b"# hidden\n"
    events = [record.getMessage() for record in caplog.records]
    assert "eval.hidden_test_overwritten" in events
    assert hidden_paths(task) == {"tests/test_calc.py"}
    assert hidden_paths(None) == frozenset()


def test_new_hidden_file_is_not_an_overwrite(suite: Suite, tmp_path: Path) -> None:
    source = suite.write_fixture("fixtures/custom-task/test_calc.py", b"# hidden\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert inject_hidden_tests(worktree, task_with_hidden(suite, source), suite.root) == []
    assert (worktree / "tests" / "test_calc.py").exists()


def test_missing_source_is_an_error(suite: Suite, tmp_path: Path) -> None:
    task = task_with_hidden(suite, "fixtures/custom-task/gone.py")
    with pytest.raises(HiddenTestError, match="source not found"):
        inject_hidden_tests(tmp_path, task, suite.root)


def test_hidden_test_is_injected_after_the_patch_and_before_checks(suite: Suite) -> None:
    """The reference diff writes a wrong tests/test_mul.py. The hidden copy must win, so it
    is copied in after the patch; and the base run fails only because the hidden test is
    already there when the checks run."""
    hidden = (suite.root / "fixtures/sample-feature-mul/hidden/tests/test_mul.py").read_bytes()
    wrong = (
        "diff --git a/src/calc/ops.py b/src/calc/ops.py\n"
        "--- a/src/calc/ops.py\n"
        "+++ b/src/calc/ops.py\n"
        "@@ -1,2 +1,6 @@\n"
        " def add(a, b):\n"
        "     return a + b\n"
        "+\n"
        "+\n"
        "+def mul(a, b):\n"
        "+    return a * b\n"
        "diff --git a/tests/test_mul.py b/tests/test_mul.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_mul.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_never_runs():\n"
        "+    raise SystemExit(9)\n"
    )
    data = suite.base_task(
        "order-task",
        repo={"path": "${SAMPLE_REPO}", "base": "${SAMPLE_C1}"},
        reference_diff=suite.write_fixture("fixtures/order-task/reference.diff", wrong.encode()),
        hidden_tests=[
            {
                "path": "tests/test_mul.py",
                "source": suite.write_fixture("fixtures/order-task/test_mul.py", hidden),
            }
        ],
        done_criteria=[
            {
                "kind": "command",
                "argv": [
                    "${PYTHON}",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_mul.py",
                ],
            }
        ],
    )
    suite.write_task(data)
    result = suite.check("order-task")
    assert result.status == "valid", result.detail
    assert result.base is not None and result.base.failing == [1]
    assert result.solution is not None and result.solution.applied_reference_diff

"""`eval check` engine: statuses, worktree cleanup, logs and the report (EVL-01)."""

import asyncio
import json
from pathlib import Path

import pytest
from evalkit import Suite
from samplerepo import git

from arpeggio_ai.evals.suite import discover

FAKE_AWS_KEY = "AKIAQ3EGRYKUZLNVHM7X"


@pytest.fixture
def suite(tmp_path: Path, home: Path) -> Suite:
    return Suite(tmp_path, home)


def arpeggio_branches(suite: Suite) -> str:
    return git(suite.repo, "branch", "--list", "arpeggio/*")


def test_valid_task(suite: Suite) -> None:
    suite.write_task(suite.base_task())
    result = suite.check("custom-task")
    assert result.status == "valid"
    assert result.base is not None and result.solution is not None
    assert result.base.commit == suite.sha("c0")
    assert result.solution.commit == suite.sha("c1")
    assert result.base.failing == [1] and result.solution.failing == []
    assert result.base.duration_s > 0
    assert len(result.base.logs) == 1
    assert result.base.logs[0].startswith("eval-check-")
    assert result.base.logs[0].endswith("/custom-task/base-check-1.log")


def test_short_shas_are_resolved(suite: Suite) -> None:
    data = suite.base_task(
        repo={
            "path": "${SAMPLE_REPO}",
            "base": suite.sha("c0")[:7],
            "solution": suite.sha("c1")[:9],
        }
    )
    suite.write_task(data)
    result = suite.check("custom-task")
    assert result.status == "valid"
    assert result.base is not None and result.base.commit == suite.sha("c0")


def test_checks_passing_at_base_are_non_discriminating(suite: Suite) -> None:
    suite.write_task(suite.base_task(done_criteria=[{"kind": "file_exists", "path": "README.md"}]))
    result = suite.check("custom-task")
    assert result.status == "non_discriminating"
    assert result.detail == "every criterion already passes at base"


def test_wrong_reference_solution_is_unsolvable(suite: Suite) -> None:
    wrong = (
        "diff --git a/src/calc/ops.py b/src/calc/ops.py\n"
        "--- a/src/calc/ops.py\n"
        "+++ b/src/calc/ops.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a * b\n"
    )
    diff = suite.write_fixture("fixtures/custom-task/reference.diff", wrong.encode())
    suite.write_task(
        suite.base_task(
            repo={"path": "${SAMPLE_REPO}", "base": "${SAMPLE_C0}"}, reference_diff=diff
        )
    )
    result = suite.check("custom-task")
    assert result.status == "unsolvable"
    assert result.solution is not None and result.solution.failing == [1]


def test_wrong_solution_commit_is_unsolvable(suite: Suite) -> None:
    repo = {"path": "${SAMPLE_REPO}", "base": "${SAMPLE_C0}", "solution": "${SAMPLE_C0}"}
    suite.write_task(suite.base_task(repo=repo))
    assert suite.check("custom-task").status == "unsolvable"


def test_reference_diff_that_does_not_apply_is_unsolvable(suite: Suite) -> None:
    diff = suite.write_fixture(
        "fixtures/custom-task/reference.diff",
        b"diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-x\n+y\n",
    )
    suite.write_task(
        suite.base_task(
            repo={"path": "${SAMPLE_REPO}", "base": "${SAMPLE_C0}"}, reference_diff=diff
        )
    )
    result = suite.check("custom-task")
    assert result.status == "unsolvable"
    assert result.detail is not None and result.detail.startswith("reference_diff does not apply")


def test_failing_setup_is_setup_failed_and_its_log_is_redacted(suite: Suite) -> None:
    script = f"print('token {FAKE_AWS_KEY}'); raise SystemExit(3)"
    suite.write_task(
        suite.base_task(setup=[{"argv": ["${PYTHON}", "-c", script], "timeout_s": 60}])
    )
    result = suite.check("custom-task")
    assert result.status == "setup_failed"
    assert result.detail == "base setup 1: exit code 3"
    assert result.base is not None and result.base.setup_failed
    assert result.solution is None
    log = suite.artifacts.read(result.base.logs[0])
    assert b"[REDACTED:aws_access_key]" in log
    assert FAKE_AWS_KEY.encode() not in log


def test_setup_that_cannot_start_is_setup_failed(suite: Suite) -> None:
    suite.write_task(suite.base_task(setup=[{"argv": ["no-such-tool-arpeggio-xyz"]}]))
    result = suite.check("custom-task")
    assert result.status == "setup_failed"
    assert "cannot" in (result.detail or "") or "not found" in (result.detail or "")


def test_setup_runs_before_checks(suite: Suite) -> None:
    script = "import pathlib; pathlib.Path('ready.txt').write_text('ok')"
    suite.write_task(
        suite.base_task(
            setup=[{"argv": ["${PYTHON}", "-c", script]}],
            done_criteria=[
                {"kind": "file_exists", "path": "ready.txt"},
                {
                    "kind": "command",
                    "argv": ["${PYTHON}", "-m", "unittest", "discover", "-s", "tests"],
                },
            ],
        )
    )
    result = suite.check("custom-task")
    assert result.status == "valid"
    assert result.base is not None and result.base.failing == [2]


def test_unknown_commit_is_an_error(suite: Suite) -> None:
    repo = {"path": "${SAMPLE_REPO}", "base": "deadbeef", "solution": "${SAMPLE_C1}"}
    suite.write_task(suite.base_task(repo=repo))
    result = suite.check("custom-task")
    assert result.status == "error"
    assert "commit deadbeef not found" in (result.detail or "")


def test_invalid_task_is_invalid_schema(suite: Suite) -> None:
    suite.write_task(suite.base_task(request="TODO: describe the task"))
    result = suite.check("custom-task")
    assert result.status == "invalid_schema"
    assert "still a TODO" in (result.detail or "")


def test_worktrees_are_removed_unless_kept(suite: Suite, home: Path) -> None:
    suite.write_task(suite.base_task())
    suite.check("custom-task")
    assert list((home / "worktrees").iterdir()) == []
    assert arpeggio_branches(suite) == ""

    result = suite.check("custom-task", keep_worktrees=True)
    assert result.base is not None and result.solution is not None
    assert result.base.worktree is not None and Path(result.base.worktree).is_dir()
    assert result.solution.worktree is not None and Path(result.solution.worktree).is_dir()
    branches = arpeggio_branches(suite)
    assert "arpeggio/eval/custom-task/" in branches and "-base" in branches
    assert "-solution" in branches


def test_report_has_statuses_but_no_output(suite: Suite) -> None:
    suite.write_task(suite.base_task())
    suite.write_task(suite.base_task("bad-task", request="TODO: x"), "holdout")
    run = suite.run()
    entries = discover(suite.root, suite.variables)

    async def check_all() -> list[object]:
        return [await run.check(entry, n) for n, entry in enumerate(entries)]

    checks = asyncio.run(check_all())
    report = run.write_report(checks)  # type: ignore[arg-type]
    assert report.parent == suite.root / ".reports"
    assert report.name.startswith("check-") and report.name.endswith("Z.json")
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["run_id"] == run.run_id
    assert data["artifacts"] == f"eval-check-{run.run_id}"
    statuses = {task["id"]: task["status"] for task in data["tasks"]}
    assert statuses["custom-task"] == "valid" and statuses["bad-task"] == "invalid_schema"
    text = report.read_text(encoding="utf-8")
    assert "output_tail" not in text and "Traceback" not in text and "AssertionError" not in text

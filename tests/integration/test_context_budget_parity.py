"""Context-selection budget parity (M0.9): the estimate describes the prompt that will run.

An eval run refuses to start when its worst-case estimate exceeds the cap, so an estimate that
disagrees with the attempt is a real defect in either direction: too low and the run overspends,
too high and a run the owner could afford is refused. The two paths read the same repository at
different times (planning has no worktree; the attempt has one), so these tests pin them equal.
"""

import asyncio
import os
from pathlib import Path

import pytest
from gitrepo import FILES, make_repo, run_git

from arpeggio_ai.evals.budget import prompt_chars, selected_context_paths
from arpeggio_ai.evals.task import EvalTask
from arpeggio_ai.orchestrator.patch import CONTEXT_MAX_FILES, build_prompt, select_context

ENV = dict(os.environ)
REQUEST = "Fix the bug in src/calc/ops.py so that calc.add adds instead of subtracting."


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    files = dict(FILES)
    files["src/calc/format.py"] = "def fmt(value):\n    return str(value)\n"
    files["tools/release/uploader.py"] = "def upload(path):\n    return path\n"
    return make_repo(tmp_path / "repo", files)


def _base(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD")


def test_planned_selection_matches_the_worktree_selection(repo: Path) -> None:
    """The paths chosen at planning time equal the paths chosen inside the worktree."""
    planned = asyncio.run(
        selected_context_paths(repo, _base(repo), REQUEST, hidden=frozenset(), env=ENV, home=repo)
    )
    in_worktree = asyncio.run(select_context(repo, REQUEST, env=ENV, home=repo))
    assert planned == in_worktree
    assert planned, "the request names a file, so something must be selected"


def test_planned_selection_respects_the_same_caps(repo: Path) -> None:
    planned = asyncio.run(
        selected_context_paths(repo, _base(repo), REQUEST, hidden=frozenset(), env=ENV, home=repo)
    )
    assert len(planned) <= CONTEXT_MAX_FILES


def test_planned_selection_excludes_hidden_paths(repo: Path) -> None:
    """A hidden test is not a candidate when planning, so it cannot be estimated or sent."""
    planned = asyncio.run(
        selected_context_paths(
            repo,
            _base(repo),
            REQUEST + " and tests/test_calc.py",
            hidden=frozenset({"tests/test_calc.py"}),
            env=ENV,
            home=repo,
        )
    )
    assert "tests/test_calc.py" not in planned


def test_planned_selection_on_a_bad_base_is_empty(repo: Path) -> None:
    """A listing that fails yields no context rather than an exception out of planning."""
    planned = asyncio.run(
        selected_context_paths(repo, "0" * 40, REQUEST, hidden=frozenset(), env=ENV, home=repo)
    )
    assert planned == []


def test_prompt_chars_uses_the_selected_paths_not_the_task_list(repo: Path) -> None:
    """``context_paths`` overrides the task's own list, so an auto run is estimated for real.

    This is the difference the milestone turns on: without the override an ``auto`` run of a
    task with no ``context_files`` would be priced as an empty prompt.
    """
    task = EvalTask(
        id="parity",
        category="bugfix",
        expected_risk="low",
        repo={"path": str(repo), "base": _base(repo), "solution": _base(repo)},
        request=REQUEST,
        done_criteria=[{"kind": "file_exists", "path": "src/calc/ops.py"}],
        context_files=[],
    )
    chosen = asyncio.run(
        selected_context_paths(repo, _base(repo), REQUEST, hidden=frozenset(), env=ENV, home=repo)
    )
    assert chosen, "the selector must have chosen something for this to be a real comparison"

    empty = prompt_chars(task, context_size=0, escalated=False)
    with_selection = prompt_chars(task, context_size=0, escalated=False, context_paths=chosen)
    # The override adds the file listing and the path names to the prompt.
    assert with_selection > empty

    # And the size it adds is exactly what the prompt builder produces for those paths.
    expected = len(build_prompt(REQUEST, ["src/calc/ops.py exists"], [(p, "") for p in chosen]))
    manual = len(build_prompt(REQUEST, ["src/calc/ops.py exists"], []))
    assert with_selection - empty == expected - manual

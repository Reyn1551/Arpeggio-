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
from arpeggio_ai.orchestrator.patch import (
    CONTEXT_CAP_BYTES,
    CONTEXT_MAX_FILES,
    build_prompt,
    select_context,
)

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


def test_the_file_cap_binds_on_both_paths(tmp_path: Path) -> None:
    """A repository with more matching files than the cap, so the cap is what stops the list.

    Without this the fixture is smaller than ``CONTEXT_MAX_FILES`` and both paths could drop the
    cap entirely and still pass: the estimate would then price a prompt the attempt never sends.
    """
    files = dict(FILES)
    # Enough files named in the request to exceed the cap several times over.
    for n in range(CONTEXT_MAX_FILES * 3):
        files[f"mods/ops{n}.py"] = f"def helper{n}(a):\n    return a + {n}\n"
    # The request names "ops", so all of these score above the noise floor.
    request = "Fix the bug in mods/ops0.py so that ops helper adds instead of subtracting."
    big = make_repo(tmp_path / "big", files)
    planned = asyncio.run(
        selected_context_paths(big, _base(big), request, hidden=frozenset(), env=ENV, home=big)
    )
    in_worktree = asyncio.run(select_context(big, request, env=ENV, home=big))
    assert len(in_worktree) == CONTEXT_MAX_FILES, "the cap must be what stopped the worktree list"
    assert len(planned) == CONTEXT_MAX_FILES, "the estimate must apply the same cap"
    assert planned == in_worktree


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


def test_the_byte_cap_binds_on_both_paths(tmp_path: Path) -> None:
    """A repository whose matching files exceed the byte cap, so bytes stop the list.

    The other parity tests use files of a few dozen bytes, so ``limit_bytes`` never binds and
    both paths could raise it without a test noticing; the estimate would then price a prompt
    the attempt never sends.
    """
    files = dict(FILES)
    # Each file is well over a third of the cap, so at most a couple can fit.
    filler = "\n".join(f"# padding line {n} to make this file large" for n in range(2000))
    for n in range(4):
        files[f"mods/ops{n}.py"] = f"def helper{n}(a):\n    return a + {n}\n{filler}\n"
    request = "Fix the bug in mods/ops0.py so that ops helper adds instead of subtracting."
    big = make_repo(tmp_path / "heavy", files)
    in_worktree = asyncio.run(select_context(big, request, env=ENV, home=big))
    planned = asyncio.run(
        selected_context_paths(big, _base(big), request, hidden=frozenset(), env=ENV, home=big)
    )
    assert planned == in_worktree
    # The cap must be what limited the result, not the number of candidates.
    assert len(in_worktree) < 4, "the byte cap must have excluded at least one candidate"
    total = sum((big / path).stat().st_size for path in in_worktree)
    assert total <= CONTEXT_CAP_BYTES


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


def test_secret_files_are_never_selected_on_either_path(tmp_path: Path) -> None:
    """A tracked secret file named by the request is not chosen: read_context would drop it,
    so choosing it only wastes a slot and the byte budget, and the estimate would count it."""
    files = dict(FILES)
    files[".env"] = "TOKEN=placeholder\n"
    files["deploy/secrets.toml"] = "token = 'placeholder'\n"
    repo = make_repo(tmp_path / "repo", files)
    request = "Load .env and deploy/secrets.toml in src/calc/ops.py before calc.add runs."
    planned = asyncio.run(
        selected_context_paths(repo, _base(repo), request, hidden=frozenset(), env=ENV, home=repo)
    )
    in_worktree = asyncio.run(select_context(repo, request, env=ENV, home=repo))
    assert planned == in_worktree
    assert "src/calc/ops.py" in planned
    assert ".env" not in planned
    assert "deploy/secrets.toml" not in planned

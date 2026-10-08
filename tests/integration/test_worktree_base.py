import asyncio
from pathlib import Path

import pytest
from gitrepo import make_repo, run_git

from arpeggio_ai.safety.process import scrubbed_env
from arpeggio_ai.safety.worktree import Worktree, WorktreeError, create_worktree, remove_worktree

ENV = scrubbed_env()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = make_repo(tmp_path / "src-repo")
    (path / "src" / "calc" / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    run_git(path, "commit", "-qam", "fix add")
    return path


@pytest.fixture
def arpeggio_home(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    return home


def create(repo: Path, home: Path, base: str | None, branch: str | None = None) -> Worktree:
    return asyncio.run(
        create_worktree(
            repo,
            home,
            task_id="01TASK",
            attempt_id="01ATTEMPT",
            attempt_seq=1,
            env=ENV,
            base=base,
            branch=branch,
        )
    )


@pytest.mark.parametrize("length", [7, 40])
def test_worktree_at_a_given_commit(repo: Path, arpeggio_home: Path, length: int) -> None:
    first = run_git(repo, "rev-parse", "HEAD~1")
    tree = create(repo, arpeggio_home, first[:length], "arpeggio/eval/demo-task/abcd1234")
    assert tree.base_sha == first
    assert tree.branch == "arpeggio/eval/demo-task/abcd1234"
    assert run_git(tree.path, "rev-parse", "HEAD") == first
    assert b"a - b" in (tree.path / "src" / "calc" / "ops.py").read_bytes()
    asyncio.run(remove_worktree(repo, arpeggio_home, tree.path, tree.branch, ENV))
    assert not tree.path.exists()


def test_default_is_still_head(repo: Path, arpeggio_home: Path) -> None:
    tree = create(repo, arpeggio_home, None)
    assert tree.base_sha == run_git(repo, "rev-parse", "HEAD")
    assert tree.branch == "arpeggio/01TASK/1"


@pytest.mark.parametrize("base", ["deadbeef", "0" * 40])
def test_unknown_commit_is_a_clear_error(repo: Path, arpeggio_home: Path, base: str) -> None:
    with pytest.raises(WorktreeError, match=f"commit {base} not found"):
        create(repo, arpeggio_home, base)
    assert not (arpeggio_home / "worktrees" / "01ATTEMPT").exists()


@pytest.mark.parametrize("base", ["HEAD", "--output=x", "abc", "main"])
def test_base_must_be_a_hex_sha(repo: Path, arpeggio_home: Path, base: str) -> None:
    with pytest.raises(WorktreeError, match="not a commit SHA"):
        create(repo, arpeggio_home, base)


def test_branch_must_be_an_arpeggio_branch(repo: Path, arpeggio_home: Path) -> None:
    with pytest.raises(WorktreeError, match="must start with arpeggio/"):
        create(repo, arpeggio_home, None, "feature/x")

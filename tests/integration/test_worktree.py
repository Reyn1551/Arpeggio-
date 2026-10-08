import asyncio
import json
from pathlib import Path

import pytest
from gitrepo import make_repo, run_git, snapshot

from arpeggio_ai.core.logs import configure_logging
from arpeggio_ai.safety import worktree as wt
from arpeggio_ai.safety.process import scrubbed_env
from arpeggio_ai.safety.worktree import (
    Worktree,
    WorktreeError,
    create_worktree,
    list_worktrees,
    parse_git_version,
    remove_worktree,
)

ENV = scrubbed_env()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "src-repo")


@pytest.fixture
def arpeggio_home(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    return home


def create(repo: Path, home: Path, attempt_id: str = "01ATTEMPT", seq: int = 1) -> Worktree:
    return asyncio.run(
        create_worktree(
            repo, home, task_id="01TASK", attempt_id=attempt_id, attempt_seq=seq, env=ENV
        )
    )


def remove(repo: Path, home: Path, path: Path, branch: str) -> None:
    asyncio.run(remove_worktree(repo, home, path, branch, ENV))


def test_worktree_is_on_its_own_branch_at_head(repo: Path, arpeggio_home: Path) -> None:
    before = snapshot(repo)
    tree = create(repo, arpeggio_home)
    assert tree.path == arpeggio_home / "worktrees" / "01ATTEMPT"
    assert tree.branch == "arpeggio/01TASK/1"
    assert tree.base_sha == run_git(repo, "rev-parse", "HEAD")
    assert tree.dirty_source is False
    assert run_git(tree.path, "rev-parse", "--abbrev-ref", "HEAD") == "arpeggio/01TASK/1"
    assert run_git(tree.path, "rev-parse", "HEAD") == tree.base_sha
    assert (
        tree.path / "src" / "calc" / "ops.py"
    ).read_bytes() == b"def add(a, b):\n    return a - b\n"
    after = snapshot(repo)
    assert {k: v for k, v in after.items() if k != "branches"} == {
        k: v for k, v in before.items() if k != "branches"
    }
    assert after["branches"] == "refs/heads/arpeggio/01TASK/1\nrefs/heads/main"


def test_dirty_source_is_logged_and_not_carried_over(repo: Path, arpeggio_home: Path) -> None:
    log_file = configure_logging(arpeggio_home / "logs")
    (repo / "src" / "calc" / "ops.py").write_text("def add(a, b):\n    return 0\n")
    (repo / "untracked.txt").write_text("scratch")
    tree = create(repo, arpeggio_home)
    assert tree.dirty_source is True
    assert (
        tree.path / "src" / "calc" / "ops.py"
    ).read_text() == "def add(a, b):\n    return a - b\n"
    assert not (tree.path / "untracked.txt").exists()
    assert (repo / "src" / "calc" / "ops.py").read_text() == "def add(a, b):\n    return 0\n"
    events = [json.loads(line)["event"] for line in log_file.read_text().splitlines()]
    assert "worktree.base_dirty" in events


def test_two_attempts_get_two_directories(repo: Path, arpeggio_home: Path) -> None:
    first = create(repo, arpeggio_home, "01A", 1)
    second = create(repo, arpeggio_home, "01B", 2)
    assert first.path != second.path and first.branch != second.branch
    (first.path / "only-first.txt").write_text("x")
    assert not (second.path / "only-first.txt").exists()
    listed = asyncio.run(list_worktrees(repo, arpeggio_home, ENV))
    assert sorted(listed) == sorted([first.path.resolve(), second.path.resolve()])


def test_existing_path_is_refused(repo: Path, arpeggio_home: Path) -> None:
    create(repo, arpeggio_home)
    with pytest.raises(WorktreeError, match="already exists"):
        create(repo, arpeggio_home, seq=2)


def test_removal_is_idempotent_and_deletes_the_branch(repo: Path, arpeggio_home: Path) -> None:
    tree = create(repo, arpeggio_home)
    (tree.path / "new.txt").write_text("untracked work")
    remove(repo, arpeggio_home, tree.path, tree.branch)
    assert not tree.path.exists()
    assert "arpeggio" not in run_git(repo, "branch", "--list")
    remove(repo, arpeggio_home, tree.path, tree.branch)  # second call is a no-op
    assert asyncio.run(list_worktrees(repo, arpeggio_home, ENV)) == []


def test_removal_refuses_paths_outside_worktrees(
    repo: Path, arpeggio_home: Path, tmp_path: Path
) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep")
    for target in (
        precious,
        arpeggio_home / "worktrees",
        arpeggio_home / "worktrees" / "a" / "b",
        repo,
    ):
        with pytest.raises(WorktreeError, match="outside"):
            remove(repo, arpeggio_home, target, "arpeggio/x/1")
    assert (precious / "keep.txt").read_text() == "keep"
    assert (repo / "README.md").exists()


def test_removal_refuses_other_branches(repo: Path, arpeggio_home: Path) -> None:
    tree = create(repo, arpeggio_home)
    with pytest.raises(WorktreeError, match="did not create"):
        remove(repo, arpeggio_home, tree.path, "main")
    assert run_git(repo, "rev-parse", "--verify", "main")


def test_not_a_repository_root(tmp_path: Path, arpeggio_home: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not the root of a git repository"):
        create(plain, arpeggio_home)


def test_subdirectory_is_not_the_root(repo: Path, arpeggio_home: Path) -> None:
    with pytest.raises(WorktreeError, match="not the root of a git repository"):
        create(repo / "src", arpeggio_home)


def test_repository_without_commits(tmp_path: Path, arpeggio_home: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    run_git(empty, "init", "-q")
    with pytest.raises(WorktreeError, match="no commits"):
        create(empty, arpeggio_home)


def test_hooks_never_run(repo: Path, arpeggio_home: Path) -> None:
    hook = repo / ".git" / "hooks" / "post-checkout"
    marker = repo.parent / "hook-ran.txt"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n")
    hook.chmod(0o755)
    create(repo, arpeggio_home)
    assert not marker.exists()


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("git version 2.55.0.windows.3", (2, 55, 0)),
        ("git version 2.30", (2, 30, 0)),
        ("git version 2.43.0", (2, 43, 0)),
    ],
)
def test_parse_git_version(text: str, version: tuple[int, int, int]) -> None:
    assert parse_git_version(text) == version


def test_unreadable_git_version() -> None:
    with pytest.raises(WorktreeError, match="cannot read the git version"):
        parse_git_version("hg version 6")


def test_old_git_is_refused(
    monkeypatch: pytest.MonkeyPatch, arpeggio_home: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(wt, "_checked_git", None)
    monkeypatch.setattr(wt, "parse_git_version", lambda text: (2, 29, 9))
    with pytest.raises(WorktreeError, match=r"git 2\.29\.9 is too old"):
        asyncio.run(wt.require_git(ENV, arpeggio_home, tmp_path))


def test_missing_git_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, arpeggio_home: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(wt, "_checked_git", None)
    env = {key: value for key, value in ENV.items() if key.upper() != "PATH"}
    env["PATH"] = str(tmp_path)
    with pytest.raises(WorktreeError, match="git is required on PATH"):
        asyncio.run(wt.require_git(env, arpeggio_home, tmp_path))

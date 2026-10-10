"""apply_edits on a real worktree (M0.8): safety checks, atomic writes and the commit."""

import asyncio
from pathlib import Path

import pytest
from gitrepo import link_directory, make_repo, run_git

from arpeggio_ai.core.logs import configure_logging
from arpeggio_ai.orchestrator.edits import EditPlan, apply_edits
from arpeggio_ai.orchestrator.patch import PatchError
from arpeggio_ai.paths import logs_dir
from arpeggio_ai.safety.process import scrubbed_env
from arpeggio_ai.safety.worktree import Worktree, create_worktree

ENV = scrubbed_env()


def block(path: str, search: str, replace: str, header: str = "FILE") -> str:
    return f"{header}: {path}\n<<<<<<< SEARCH\n{search}=======\n{replace}>>>>>>> REPLACE\n"


FIX = block("src/calc/ops.py", "    return a - b\n", "    return a + b\n")


@pytest.fixture
def tree(tmp_path: Path, home: Path) -> Worktree:
    home.mkdir(parents=True, exist_ok=True)
    configure_logging(logs_dir(home))
    repo = make_repo(tmp_path / "repo")
    return asyncio.run(
        create_worktree(repo, home, task_id="01T", attempt_id="01A", attempt_seq=1, env=ENV)
    )


def apply(reply: str, tree: Worktree, home: Path, plans: list[EditPlan] | None = None) -> str:
    return asyncio.run(
        apply_edits(
            reply,
            tree.path,
            edit_format="search-replace",
            attempt_id="01A",
            env=ENV,
            home=home,
            on_planned=None if plans is None else plans.append,
        )
    )


def failure(reply: str, tree: Worktree, home: Path) -> PatchError:
    with pytest.raises(PatchError) as caught:
        apply(reply, tree, home)
    return caught.value


def assert_untouched(tree: Worktree) -> None:
    assert run_git(tree.path, "status", "--porcelain") == ""
    assert run_git(tree.path, "rev-parse", "HEAD") == tree.base_sha


def test_edit_is_applied_and_committed(tree: Worktree, home: Path) -> None:
    plans: list[EditPlan] = []
    sha = apply(FIX, tree, home, plans)
    ops = tree.path / "src" / "calc" / "ops.py"
    assert ops.read_bytes() == b"def add(a, b):\n    return a + b\n"
    assert run_git(tree.path, "rev-parse", "HEAD") == sha
    assert run_git(tree.path, "rev-parse", "HEAD^") == tree.base_sha
    assert run_git(tree.path, "log", "-1", "--format=%an <%ae>|%s") == (
        "Arpeggio <arpeggio@localhost>|arpeggio: attempt 01A"
    )
    assert run_git(tree.path, "status", "--porcelain") == ""
    [plan] = plans
    assert plan.expected == {"src/calc/ops.py": "lf"} and plan.shrink == 0.0


def test_new_file_is_created(tree: Worktree, home: Path) -> None:
    apply(block("src/calc/extra.py", "", "VALUE = 1\n", header="NEW FILE"), tree, home)
    assert (tree.path / "src" / "calc" / "extra.py").read_bytes() == b"VALUE = 1\n"


def test_one_failing_block_writes_nothing(tree: Worktree, home: Path) -> None:
    reply = FIX + block("README.md", "# missing\n", "# demo\n")
    error = failure(reply, tree, home)
    assert error.reason == "patch_search_not_found" and "README.md" in error.detail
    assert_untouched(tree)


def test_a_new_file_and_a_failing_block_write_nothing(tree: Worktree, home: Path) -> None:
    reply = block("src/new.py", "", "x = 1\n", header="NEW FILE") + block(
        "README.md", "nope\n", "x\n"
    )
    assert failure(reply, tree, home).reason == "patch_search_not_found"
    assert not (tree.path / "src" / "new.py").exists()
    assert_untouched(tree)


def test_unknown_path_is_rejected(tree: Worktree, home: Path) -> None:
    error = failure(block("src/Calc/ops.py", "    return a - b\n", "x\n"), tree, home)
    assert error.reason == "patch_unknown_path" and "src/Calc/ops.py" in error.detail
    assert_untouched(tree)


def test_untracked_file_is_unknown(tree: Worktree, home: Path) -> None:
    (tree.path / "notes.txt").write_text("a\n", encoding="utf-8")
    assert failure(block("notes.txt", "a\n", "b\n"), tree, home).reason == "patch_unknown_path"


def test_new_file_that_exists_is_malformed(tree: Worktree, home: Path) -> None:
    error = failure(block("README.md", "", "x\n", header="NEW FILE"), tree, home)
    assert error.reason == "patch_malformed" and "already exists" in error.detail
    assert_untouched(tree)


@pytest.mark.parametrize(
    "path", ["../escape.py", "/abs.py", ".git/config", "src/../../escape.py", '"quoted.py']
)
def test_unsafe_paths(tree: Worktree, home: Path, path: str) -> None:
    error = failure(block(path, "", "x\n", header="NEW FILE"), tree, home)
    assert error.reason == "patch_unsafe"
    assert_untouched(tree)


def test_path_through_a_linked_directory_is_unsafe(
    tree: Worktree, home: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link_directory(outside, tree.path / "escape")
    error = failure(block("escape/pwned.txt", "", "x\n", header="NEW FILE"), tree, home)
    assert error.reason == "patch_unsafe" and "outside the worktree" in error.detail
    assert list(outside.iterdir()) == []


def test_redaction_placeholder_is_rejected_before_anything_else(tree: Worktree, home: Path) -> None:
    reply = block("src/Unknown.py", "x\n", "KEY = '[REDACTED:api_key]'\n")
    error = failure(reply, tree, home)
    assert error.reason == "patch_writes_redacted_placeholder"
    assert error.detail.endswith("src/Unknown.py:block 1 line 1")
    assert "KEY" not in error.detail


def test_binary_file_is_unsafe(tmp_path: Path, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    repo = make_repo(tmp_path / "repo", {"data.bin": "a\0b\n"})
    tree = asyncio.run(
        create_worktree(repo, home, task_id="01T", attempt_id="01A", attempt_seq=1, env=ENV)
    )
    error = failure(block("data.bin", "a\0b\n", "c\n"), tree, home)
    assert error.reason == "patch_unsafe" and "binary" in error.detail


def test_crlf_file_stays_crlf(tmp_path: Path, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    repo = make_repo(tmp_path / "repo", {"win.txt": "one\r\ntwo\r\nthree\r\n"})
    tree = asyncio.run(
        create_worktree(repo, home, task_id="01T", attempt_id="01A", attempt_seq=1, env=ENV)
    )
    apply(block("win.txt", "two\n", "2\n2b\n"), tree, home)
    assert (tree.path / "win.txt").read_bytes() == b"one\r\n2\r\n2b\r\nthree\r\n"


def test_shrink_is_planned(tmp_path: Path, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    repo = make_repo(tmp_path / "repo", {"long.txt": "a\nb\nc\nd\n"})
    tree = asyncio.run(
        create_worktree(repo, home, task_id="01T", attempt_id="01A", attempt_seq=1, env=ENV)
    )
    plans: list[EditPlan] = []
    apply(block("long.txt", "b\nc\nd\n", ""), tree, home, plans)
    assert plans[0].shrink == 0.75


def test_a_failed_write_restores_what_was_written(
    tree: Worktree, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.write_bytes

    def flaky(self: Path, data: bytes) -> int:
        if self.name == "README.md" and data != b"# demo\n":
            raise OSError("disk full")
        return original(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    reply = (
        FIX
        + block("src/calc/extra.py", "", "x = 1\n", header="NEW FILE")
        + block("README.md", "# demo\n", "# changed\n")
    )
    error = failure(reply, tree, home)
    assert error.reason == "patch_does_not_apply" and "disk full" in error.detail
    assert not (tree.path / "src" / "calc" / "extra.py").exists()
    assert_untouched(tree)

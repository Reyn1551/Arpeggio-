import asyncio
import json
from pathlib import Path

import pytest
from gitrepo import FIX, link_directory, make_repo, run_git

from arpeggio_ai.core.logs import configure_logging
from arpeggio_ai.orchestrator.patch import PatchError, apply_patch, extract_patch, read_context
from arpeggio_ai.safety.process import scrubbed_env
from arpeggio_ai.safety.worktree import Worktree, create_worktree

ENV = scrubbed_env()

CONFLICT = """diff --git a/src/calc/ops.py b/src/calc/ops.py
--- a/src/calc/ops.py
+++ b/src/calc/ops.py
@@ -1,2 +1,2 @@
 def add(x, y):
-    return x - y
+    return x + y
"""


@pytest.fixture
def tree(tmp_path: Path, home: Path) -> Worktree:
    home.mkdir(parents=True, exist_ok=True)
    repo = make_repo(tmp_path / "repo")
    return asyncio.run(
        create_worktree(repo, home, task_id="01T", attempt_id="01A", attempt_seq=1, env=ENV)
    )


def apply(diff: str, tree: Worktree, home: Path) -> str:
    return asyncio.run(apply_patch(diff, tree.path, attempt_id="01A", env=ENV, home=home))


def failure(diff: str, tree: Worktree, home: Path) -> PatchError:
    with pytest.raises(PatchError) as caught:
        apply(diff, tree, home)
    return caught.value


def test_valid_diff_is_applied_and_committed(tree: Worktree, home: Path) -> None:
    sha = apply(extract_patch(FIX), tree, home)
    assert (
        tree.path / "src" / "calc" / "ops.py"
    ).read_text() == "def add(a, b):\n    return a + b\n"
    assert run_git(tree.path, "rev-parse", "HEAD") == sha
    assert run_git(tree.path, "rev-parse", "HEAD^") == tree.base_sha
    assert run_git(tree.path, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%s") == (
        "Arpeggio <arpeggio@localhost>|Arpeggio <arpeggio@localhost>|arpeggio: attempt 01A"
    )
    assert run_git(tree.path, "status", "--porcelain") == ""
    assert run_git(tree.path, "rev-parse", "--abbrev-ref", "HEAD") == tree.branch


def test_new_file_is_applied(tree: Worktree, home: Path) -> None:
    diff = "--- /dev/null\n+++ b/src/calc/extra.py\n@@ -0,0 +1 @@\n+VALUE = 1\n"
    apply(diff, tree, home)
    assert (tree.path / "src" / "calc" / "extra.py").read_text() == "VALUE = 1\n"


def test_conflicting_diff_does_not_apply(tree: Worktree, home: Path) -> None:
    error = failure(CONFLICT, tree, home)
    assert error.reason == "patch_does_not_apply"
    assert "src/calc/ops.py" in error.detail
    assert run_git(tree.path, "status", "--porcelain") == ""
    assert run_git(tree.path, "rev-parse", "HEAD") == tree.base_sha


def test_path_through_a_linked_directory_is_unsafe(
    tree: Worktree, home: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link_directory(outside, tree.path / "escape")
    diff = "--- /dev/null\n+++ b/escape/pwned.txt\n@@ -0,0 +1 @@\n+x\n"
    error = failure(diff, tree, home)
    assert (error.reason, error.detail) == (
        "patch_unsafe",
        "escape/pwned.txt: resolves outside the worktree",
    )
    assert not (outside / "pwned.txt").exists()


def test_changes_outside_the_declared_paths_are_refused(tree: Worktree, home: Path) -> None:
    (tree.path / "stray.txt").write_text("not from the diff")
    error = failure(extract_patch(FIX), tree, home)
    assert error.reason == "patch_unsafe"
    assert "stray.txt" in error.detail


def test_a_no_op_diff_changes_nothing(tree: Worktree, home: Path) -> None:
    error = failure("--- a/README.md\n+++ b/README.md\n", tree, home)
    assert error.reason == "patch_does_not_apply"


def test_context_files_skip_secrets_missing_and_oversize(tree: Worktree, home: Path) -> None:
    log_file = configure_logging(home / "logs")
    (tree.path / ".env").write_text("TOKEN=secret")
    (tree.path / "big.txt").write_text("x" * 200)
    (tree.path / "bin.dat").write_bytes(b"\xff\xfe\x00")
    chosen = read_context(
        tree.path,
        ["src/calc/ops.py", ".env", "missing.py", "../etc/passwd", "big.txt", "bin.dat"],
        cap_bytes=150,
    )
    assert chosen == [("src/calc/ops.py", "def add(a, b):\n    return a - b\n")]
    reasons = {
        line["path"]: line["reason"]
        for line in map(json.loads, log_file.read_text().splitlines())
        if line["event"] == "patch.context_skipped"
    }
    assert reasons == {
        ".env": "matches a secret file pattern",
        "missing.py": "not a file in the worktree",
        "../etc/passwd": "must not contain empty, '.' or '..' segments",
        "big.txt": "over the 150-byte context cap",
        "bin.dat": "not UTF-8 text",
    }

"""Temporary git repositories for tests: a tiny Python package with one bug.

``calc.add`` subtracts instead of adding. ``tests/test_ok.py`` passes as is and
``tests/test_calc.py`` fails until the bug is fixed. Files are written with LF endings and
committed with ``core.autocrlf=false``, so the bytes are the same on every platform.
"""

import os
import subprocess
import sys
from pathlib import Path

FILES = {
    "README.md": "# demo\n",
    "src/calc/__init__.py": "",
    "src/calc/ops.py": "def add(a, b):\n    return a - b\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "tests/test_calc.py": (
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))\n"
        "\n"
        "from calc.ops import add\n"
        "\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
    ),
}

FIX = """```diff
diff --git a/src/calc/ops.py b/src/calc/ops.py
--- a/src/calc/ops.py
+++ b/src/calc/ops.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
```"""

WRONG_FIX = """```diff
diff --git a/src/calc/ops.py b/src/calc/ops.py
--- a/src/calc/ops.py
+++ b/src/calc/ops.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a * b
```"""


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "core.autocrlf=false",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def make_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True)
    run_git(path, "init", "-q", "-b", "main")
    for name, text in (files or FILES).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    run_git(path, "add", "-A")
    run_git(path, "commit", "-q", "-m", "initial")
    return path


def snapshot(repo: Path) -> dict[str, object]:
    """Branches, HEAD, index and working files: everything a worktree must not change."""
    files = {
        str(p.relative_to(repo)): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }
    return {
        "head": run_git(repo, "rev-parse", "HEAD"),
        "branch": run_git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "branches": run_git(repo, "for-each-ref", "--format=%(refname)", "refs/heads"),
        "index": run_git(repo, "ls-files", "-s"),
        "status": run_git(repo, "status", "--porcelain"),
        "files": files,
    }


def link_directory(target: Path, link: Path) -> None:
    """A directory symlink, or a junction on Windows where symlinks need a privilege."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))

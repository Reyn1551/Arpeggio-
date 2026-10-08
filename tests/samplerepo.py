"""The repository behind the public sample eval suite (``evals/sample/``).

A tiny ``calc`` package with ``unittest`` tests, built with fixed authors and dates so the
commits are the same everywhere:

- ``c0`` base: ``add`` subtracts, ``calc.toml`` has ``precision = 0``
- ``c1`` fixes ``add`` (solution of ``sample-fix-add``)
- ``c2`` adds ``mul`` and ``tests/test_mul.py`` (solution of ``sample-feature-mul``)
- ``c3`` adds ``docs/usage.md`` and sets ``precision = 2``. ``sample-docs-config`` uses
  only its diff, committed as ``fixtures/sample-docs-config/reference.diff``.

``sample_variables`` gives the ``${NAME}`` values the sample task files use.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

SAMPLE_SUITE = Path(__file__).resolve().parent.parent / "evals" / "sample"

_HEADER = (
    "import sys\n"
    "import unittest\n"
    "from pathlib import Path\n"
    "\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))\n"
    "\n"
)
C0 = {
    "README.md": "# calc\n\nA tiny calculator.\n",
    "calc.toml": "[calc]\nprecision = 0\n",
    "src/calc/__init__.py": "",
    "src/calc/ops.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": _HEADER
    + "from calc.ops import add\n\n\n"
    + "class AddTest(unittest.TestCase):\n"
    + "    def test_add(self):\n"
    + "        self.assertEqual(add(2, 3), 5)\n",
}
C1 = {"src/calc/ops.py": "def add(a, b):\n    return a + b\n"}
C2 = {
    "src/calc/ops.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
    "tests/test_mul.py": _HEADER
    + "from calc.ops import mul\n\n\n"
    + "class MulTest(unittest.TestCase):\n"
    + "    def test_mul(self):\n"
    + "        self.assertEqual(mul(4, 5), 20)\n",
}
C3 = {
    "calc.toml": "[calc]\nprecision = 2\n",
    "docs/usage.md": "# Usage\n\n`calc.ops.add(a, b)` and `calc.ops.mul(a, b)`.\n",
}


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Sample",
        "GIT_AUTHOR_EMAIL": "sample@example.com",
        "GIT_COMMITTER_NAME": "Sample",
        "GIT_COMMITTER_EMAIL": "sample@example.com",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    }
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8").strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_sample_repo(path: Path) -> dict[str, str]:
    """Create the repository at ``path`` and return the commit SHAs, c0 to c3."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    return {
        "c0": _commit(path, C0, "calc with a broken add"),
        "c1": _commit(path, C1, "fix add"),
        "c2": _commit(path, C2, "add mul"),
        "c3": _commit(path, C3, "document usage and set precision"),
    }


def sample_variables(repo: Path, commits: dict[str, str]) -> dict[str, str]:
    return {
        "SAMPLE_REPO": repo.resolve().as_posix(),
        "PYTHON": Path(sys.executable).as_posix(),
        **{f"SAMPLE_{name.upper()}": sha for name, sha in commits.items()},
    }


def copy_sample_suite(target: Path) -> Path:
    """A writable copy of ``evals/sample`` (``eval check`` writes reports into it)."""
    shutil.copytree(SAMPLE_SUITE, target, ignore=shutil.ignore_patterns(".reports"))
    return target


def materialize(root: Path, variables: dict[str, str]) -> None:
    """Replace ``${NAME}`` in the task files under ``root`` so the CLI can load them."""
    for file in root.glob("*/*.yaml"):
        text = file.read_text(encoding="utf-8")
        for name, value in variables.items():
            text = text.replace("${" + name + "}", value)
        file.write_text(text, encoding="utf-8")


def sample_suite(tmp: Path) -> tuple[Path, Path, dict[str, str]]:
    """The generated repo, a writable copy of the sample suite and its variables."""
    repo = tmp / "sample-repo"
    commits = make_sample_repo(repo)
    return repo, copy_sample_suite(tmp / "evals"), sample_variables(repo, commits)

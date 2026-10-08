"""Attempt worktrees (EXE-04): one git worktree per attempt, on its own branch.

A worktree lives at ``<home>/worktrees/<attempt_id>`` (short, for Windows ``MAX_PATH``) on
branch ``arpeggio/<task_id>/<attempt_seq>``, created at the source repo's ``HEAD`` commit.
Eval self-checks pass an explicit ``base`` commit (a full or abbreviated SHA that must
resolve to a commit) and their own branch name, ``arpeggio/eval/<task_id>/<run>``.
Uncommitted changes in the user's working copy are not carried over: the worktree starts
from the last commit, and a dirty source logs ``worktree.base_dirty``. The user's working
copy and branches are never modified.

Every git command here runs with hooks disabled, ``core.autocrlf=false`` and commit signing
off, so nothing from the user's git setup runs and the worktree holds files exactly as
committed (ADR-0008).
"""

import logging
import os
import re
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from arpeggio_ai.core.errors import ArpeggioError
from arpeggio_ai.safety.process import ProcessError, ProcessResult, run_process

log = logging.getLogger(__name__)

GIT_MIN = (2, 30)
GIT_TIMEOUT_S = 120.0
BRANCH_PREFIX = "arpeggio/"
_SHA = re.compile(r"[0-9a-fA-F]{4,40}")
_VERSION = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")
_checked_git: tuple[int, int, int] | None = None


class WorktreeError(ArpeggioError):
    """A worktree could not be created, removed or listed."""


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    branch: str
    base_sha: str
    dirty_source: bool


def worktrees_root(home: Path) -> Path:
    return home / "worktrees"


def _hardening(home: Path) -> list[str]:
    # core.hooksPath points at a directory that never exists, so no hook can run.
    return [
        "-c",
        f"core.hooksPath={home / '.no-hooks'}",
        "-c",
        "core.autocrlf=false",
        "-c",
        "commit.gpgsign=false",
    ]


async def git(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    home: Path,
    timeout_s: float = GIT_TIMEOUT_S,
) -> ProcessResult:
    """Run one hardened git command. Raises WorktreeError if git is missing."""
    try:
        return await run_process(
            ["git", *_hardening(home), *args], cwd=cwd, env=env, timeout_s=timeout_s
        )
    except ProcessError as error:
        raise WorktreeError(f"git is required on PATH: {error}") from None


def _ok(result: ProcessResult, what: str) -> str:
    if result.timed_out:
        raise WorktreeError(f"{what}: git timed out")
    if result.exit_code != 0:
        raise WorktreeError(f"{what}: {result.text().strip()[-1000:]}")
    return result.text().strip()


def parse_git_version(text: str) -> tuple[int, int, int]:
    match = _VERSION.search(text)
    if match is None:
        raise WorktreeError(f"cannot read the git version from {text.strip()!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


async def require_git(env: Mapping[str, str], home: Path, cwd: Path) -> tuple[int, int, int]:
    """Check once per process that git exists and is at least 2.30."""
    global _checked_git
    if _checked_git is None:
        version = parse_git_version(
            _ok(await git(["--version"], cwd=cwd, env=env, home=home), "git --version")
        )
        if version[:2] < GIT_MIN:
            found = ".".join(map(str, version))
            raise WorktreeError(f"git {found} is too old; Arpeggio needs git 2.30 or newer")
        _checked_git = version
    return _checked_git


async def create_worktree(
    repo: Path,
    home: Path,
    *,
    task_id: str,
    attempt_id: str,
    attempt_seq: int,
    env: Mapping[str, str],
    base: str | None = None,
    branch: str | None = None,
) -> Worktree:
    """Create the attempt's worktree on a new branch at ``base``, or at HEAD when it is None.

    ``branch`` overrides the default ``arpeggio/<task_id>/<attempt_seq>`` name and must
    start with ``arpeggio/``.
    """
    repo = repo.resolve()
    await require_git(env, home, repo)
    top = await git(["rev-parse", "--show-toplevel"], cwd=repo, env=env, home=home)
    if top.exit_code != 0 or Path(top.text().strip()).resolve() != repo:
        raise WorktreeError(f"not the root of a git repository: {repo}")
    if base is None:
        head = await git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=repo, env=env, home=home)
        if head.exit_code != 0:
            raise WorktreeError(f"repository has no commits yet: {repo}")
        base_sha = head.text().strip()
    else:
        base_sha = await resolve_commit(repo, base, home=home, env=env)

    status = _ok(await git(["status", "--porcelain"], cwd=repo, env=env, home=home), "git status")
    dirty = bool(status)
    if dirty:
        log.warning("worktree.base_dirty", extra={"repo": str(repo), "base_sha": base_sha})

    root = worktrees_root(home)
    root.mkdir(parents=True, exist_ok=True)
    path = root / attempt_id
    if path.exists():
        raise WorktreeError(f"worktree path already exists: {path}")
    if branch is None:
        branch = f"{BRANCH_PREFIX}{task_id}/{attempt_seq}"
    elif not branch.startswith(BRANCH_PREFIX):
        raise WorktreeError(f"worktree branch must start with {BRANCH_PREFIX}: {branch}")
    _ok(
        await git(
            ["worktree", "add", "-b", branch, str(path), base_sha], cwd=repo, env=env, home=home
        ),
        "git worktree add",
    )
    log.info("worktree.created", extra={"path": str(path), "branch": branch})
    return Worktree(path=path, branch=branch, base_sha=base_sha, dirty_source=dirty)


async def resolve_commit(repo: Path, ref: str, *, home: Path, env: Mapping[str, str]) -> str:
    """The full SHA of commit ``ref`` (a full or abbreviated hex SHA) in ``repo``."""
    if _SHA.fullmatch(ref) is None:
        raise WorktreeError(f"not a commit SHA (4 to 40 hex characters): {ref!r}")
    result = await git(
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo.resolve(),
        env=env,
        home=home,
    )
    if result.exit_code != 0:
        raise WorktreeError(f"commit {ref} not found (or ambiguous) in {repo}")
    return result.text().strip()


def _inside_root(path: Path, home: Path) -> Path:
    root = worktrees_root(home).resolve()
    target = path.resolve()
    if target.parent != root:
        raise WorktreeError(f"refusing to touch a path outside {root}: {path}")
    return target


def _force_remove(function: object, path: str, _exc: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE)  # git marks some files read-only on Windows
    if os.path.isdir(path):
        os.rmdir(path)
    else:
        os.remove(path)


async def remove_worktree(
    repo: Path, home: Path, path: Path, branch: str, env: Mapping[str, str]
) -> None:
    """Remove an attempt worktree and its branch. Safe to call more than once."""
    target = _inside_root(path, home)
    if not branch.startswith(BRANCH_PREFIX):
        raise WorktreeError(f"refusing to delete a branch Arpeggio did not create: {branch}")
    repo = repo.resolve()
    await git(["worktree", "remove", "--force", str(target)], cwd=repo, env=env, home=home)
    await git(["worktree", "prune"], cwd=repo, env=env, home=home)
    await git(["branch", "-D", branch], cwd=repo, env=env, home=home)
    if target.exists():
        shutil.rmtree(target, onexc=_force_remove)
    log.info("worktree.removed", extra={"path": str(target), "branch": branch})


async def list_worktrees(repo: Path, home: Path, env: Mapping[str, str]) -> list[Path]:
    """Arpeggio's worktrees of ``repo`` (those under ``<home>/worktrees``)."""
    text = _ok(
        await git(["worktree", "list", "--porcelain"], cwd=repo.resolve(), env=env, home=home),
        "git worktree list",
    )
    root = worktrees_root(home).resolve()
    paths = [
        Path(line[len("worktree ") :]) for line in text.splitlines() if line.startswith("worktree ")
    ]
    return [path.resolve() for path in paths if path.resolve().parent == root]

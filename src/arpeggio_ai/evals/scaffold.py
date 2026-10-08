"""``arpeggio eval new``: scaffold a task from a base commit and a solution commit.

Files changed between the two commits that match a test glob become hidden tests: their
solution-commit contents go to ``fixtures/<id>/hidden/<path>``. The rest of the change is
written as ``fixtures/<id>/reference.diff``. The task file starts with a TODO ``request``
and no ``done_criteria``, so ``eval check`` reports it ``invalid_schema`` until the owner
fills both in. Every file written is passed through the secret scanner and only counts per
type are reported.

``request``, ``category`` and ``risk`` fill those fields directly. ``text_check`` adds a
generated Node hidden test that asserts a regular expression matches one file, after
verifying it fails at base and passes at solution (``evals/textcheck.py``), and makes
``node --test <that test>`` the done criterion.
"""

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from arpeggio_ai.core.errors import ArpeggioError
from arpeggio_ai.evals.task import SPLIT_DIRS, TASK_ID, TODO_REQUEST, Category, Risk, Split
from arpeggio_ai.evals.textcheck import TextCheck, TextCheckError, hidden_path, render, verify
from arpeggio_ai.safety.fileset import is_binary, matches_any
from arpeggio_ai.safety.process import ProcessResult
from arpeggio_ai.safety.secret_scan import SecretScanner
from arpeggio_ai.safety.worktree import git, resolve_commit

DEFAULT_TEST_GLOBS = (
    "tests/**",
    "test/**",
    "**/*Test.php",
    "**/test_*.py",
    "**/*_test.py",
    "**/*.spec.*",
    "**/*.test.*",
)

_TEMPLATE = """\
# Eval task created by `arpeggio eval new`. Fill in `request` and `done_criteria`, then run
# `arpeggio eval check --id {id}`. Field reference: docs/06-EVALUATION.md.
id: {id}
# category: feature | bugfix | refactor | docs | config | data_pipeline | ml_experiment | other
category: {category}
expected_risk: {risk}       # low | medium | high
repo:
  path: {path}
  base: {base}
  solution: {solution}
request: {request}
# setup:                 # optional, runs in the worktree before the checks
#   - argv: ["composer", "install", "--no-interaction"]
#     timeout_s: 600
done_criteria:{done}
# done_criteria:         # at least one; argv is a list, never a shell string
#   - kind: command
#     argv: ["uv", "run", "pytest", "tests/test_example.py"]
#   - kind: file_exists
#     path: src/pkg/new_module.py
hidden_tests:{hidden}
reference_diff: {reference}
context_files: []        # optional hints for prompts; never a hidden test path
tags: []
notes: null              # never sent to a model
"""


class ScaffoldError(ArpeggioError):
    """``eval new`` refused to write the task."""


@dataclass(slots=True)
class NewTask:
    id: str
    split: Split
    task_file: Path
    hidden_tests: list[str]
    reference_diff: str | None
    files_scanned: int = 0
    findings: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "split": self.split,
            "task_file": str(self.task_file),
            "hidden_tests": self.hidden_tests,
            "reference_diff": self.reference_diff,
            "scan": {"files": self.files_scanned, "findings_by_type": self.findings},
        }


def _q(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid double-quoted YAML scalar


def _ok(result: ProcessResult, what: str) -> bytes:
    if result.exit_code != 0 or result.timed_out or result.output_limit_exceeded:
        raise ScaffoldError(f"{what} failed: {result.text().strip()[:500]}")
    return result.output


def _changes(listing: bytes) -> list[tuple[str, str]]:
    parts = listing.decode("utf-8", errors="surrogateescape").split("\0")
    return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2) if parts[i]]


async def _text_at(
    repo: Path, sha: str, path: str, *, env: Mapping[str, str], home: Path
) -> str | None:
    """The file's text at ``sha``, or None if it does not exist there."""
    result = await git(["show", f"{sha}:{path}"], cwd=repo, env=env, home=home)
    if result.exit_code != 0:
        return None
    return result.output.decode("utf-8", errors="replace")


async def scaffold(
    repo: Path,
    base: str,
    solution: str,
    task_id: str,
    evals_root: Path,
    *,
    home: Path,
    env: Mapping[str, str],
    scanner: SecretScanner,
    holdout: bool = False,
    test_globs: Sequence[str] = DEFAULT_TEST_GLOBS,
    force: bool = False,
    request: str | None = None,
    category: Category = "other",
    risk: Risk = "low",
    text_check: TextCheck | None = None,
) -> NewTask:
    if TASK_ID.fullmatch(task_id) is None:
        raise ScaffoldError(f"invalid task id {task_id!r}: must match ^[a-z0-9][a-z0-9-]{{2,63}}$")
    split: Split = "holdout" if holdout else "tuning"
    other: Split = "tuning" if holdout else "holdout"
    task_file = evals_root / SPLIT_DIRS[split] / f"{task_id}.yaml"
    if (evals_root / SPLIT_DIRS[other] / f"{task_id}.yaml").exists():
        raise ScaffoldError(f"task {task_id} already exists in the {other} split")
    if task_file.exists() and not force:
        raise ScaffoldError(f"task {task_id} already exists: {task_file} (use --force)")

    repo = repo.resolve()
    top = await git(["rev-parse", "--show-toplevel"], cwd=repo, env=env, home=home)
    if top.exit_code != 0 or Path(top.text().strip()).resolve() != repo:
        raise ScaffoldError(f"not the root of a git repository: {repo}")
    base_sha = await resolve_commit(repo, base, home=home, env=env)
    solution_sha = await resolve_commit(repo, solution, home=home, env=env)

    listing = _ok(
        await git(
            ["diff", "--name-status", "-z", "--no-renames", base_sha, solution_sha],
            cwd=repo,
            env=env,
            home=home,
        ),
        "git diff --name-status",
    )
    changes = _changes(listing)
    if not changes:
        raise ScaffoldError(f"no changes between {base_sha[:12]} and {solution_sha[:12]}")
    hidden = [path for status, path in changes if status != "D" and matches_any(test_globs, path)]
    contents = {
        path: _ok(
            await git(["show", f"{solution_sha}:{path}"], cwd=repo, env=env, home=home),
            f"git show {path}",
        )
        for path in hidden
    }
    generated: bytes | None = None
    if text_check is not None:
        at_base = await _text_at(repo, base_sha, text_check.file, env=env, home=home)
        at_solution = await _text_at(repo, solution_sha, text_check.file, env=env, home=home)
        try:
            verify(text_check, at_base, at_solution)
        except TextCheckError as error:
            raise ScaffoldError(str(error)) from None
        generated = render(task_id, text_check)
    excludes = [f":(exclude,literal){path}" for path in hidden]
    diff = _ok(
        await git(
            [
                "diff",
                "--no-renames",
                "--no-color",
                "--no-ext-diff",
                base_sha,
                solution_sha,
                "--",
                ".",
                *excludes,
            ],
            cwd=repo,
            env=env,
            home=home,
        ),
        "git diff",
    )

    fixtures = evals_root / "fixtures" / task_id
    if fixtures.exists():
        if not force:
            raise ScaffoldError(f"fixtures already exist: {fixtures} (use --force)")
        shutil.rmtree(fixtures)
    written: list[Path] = []
    entries = []
    for path, data in contents.items():
        source = f"fixtures/{task_id}/hidden/{path}"
        target = evals_root / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(target)
        entries.append(f"\n  - path: {_q(path)}\n    source: {_q(source)}")
    done = " []"
    if generated is not None:
        path = hidden_path(task_id)
        source = f"fixtures/{task_id}/hidden/{path}"
        target = evals_root / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(generated)
        written.append(target)
        hidden.append(path)
        entries.append(f"\n  - path: {_q(path)}\n    source: {_q(source)}")
        argv = ", ".join(_q(arg) for arg in ("node", "--test", path))
        done = f"\n  - kind: command\n    argv: [{argv}]"
    reference = None
    if diff.strip():
        reference = f"fixtures/{task_id}/reference.diff"
        target = evals_root / reference
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(diff)
        written.append(target)

    text = _TEMPLATE.format(
        id=task_id,
        path=_q(repo.as_posix()),
        base=_q(base_sha),
        solution=_q(solution_sha),
        request=_q(TODO_REQUEST if request is None else request),
        category=category,
        risk=risk,
        done=done,
        hidden="".join(entries) if entries else " []",
        reference="null" if reference is None else _q(reference),
    )
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(text, encoding="utf-8", newline="\n")
    written.append(task_file)

    result = NewTask(task_id, split, task_file, hidden, reference)
    for file in written:
        data = file.read_bytes()
        if is_binary(data):
            continue
        result.files_scanned += 1
        found = scanner.scan(data.decode("utf-8", errors="replace"), file.name).findings
        for finding in found:
            result.findings[finding.type] = result.findings.get(finding.type, 0) + 1
    result.findings = dict(sorted(result.findings.items()))
    return result

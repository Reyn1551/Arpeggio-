"""Eval task files (EVL-01): the YAML format, its Pydantic model and the loader.

A task lives at ``<evals>/tasks/<id>.yaml`` (tuning split) or ``<evals>/holdout/<id>.yaml``.
It names a local git repository, the commit before the change (``base``) and the human
solution, either as a commit (``solution``) or as a diff under the evals directory
(``reference_diff``). ``hidden_tests`` are files copied into the worktree only for
verification, never shown to a model. See docs/06-EVALUATION.md for every field.

Files are read with ``yaml.safe_load`` only (ADR-0009). ``${NAME}`` placeholders in string
values are replaced only when the caller passes a mapping: the public sample suite uses
this in tests to point at a generated repository. The CLI never passes one.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from arpeggio_ai.core.errors import ArpeggioError
from arpeggio_ai.verify.criteria import (
    CommandCriterion,
    Criterion,
    FileExistsCriterion,
    relative_path_problem,
)

TASK_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,63}")
SHA = re.compile(r"[0-9a-fA-F]{4,40}")
REQUEST_MIN_CHARS = 10
REQUEST_MAX_CHARS = 4000
TODO_REQUEST = "TODO: describe the task"
Split = Literal["tuning", "holdout"]
SPLIT_DIRS: dict[Split, str] = {"tuning": "tasks", "holdout": "holdout"}
Category = Literal[
    "feature", "bugfix", "refactor", "docs", "config", "data_pipeline", "ml_experiment", "other"
]
Risk = Literal["low", "medium", "high"]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]

_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


class TaskError(ArpeggioError):
    """A task file cannot be read or is invalid. ``problems`` lists every issue found."""

    def __init__(self, file: Path, problems: list[str]) -> None:
        super().__init__(f"{file}: " + "; ".join(problems))
        self.file = file
        self.problems = problems


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _relative(value: str) -> str:
    problem = relative_path_problem(value)
    if problem is not None:
        raise PydanticCustomError("relative_path", problem)
    return value


def _sha(value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool):
        raise PydanticCustomError("sha_number", "quote the commit SHA so YAML keeps it a string")
    if isinstance(value, str) and SHA.fullmatch(value) is None:
        raise PydanticCustomError("sha", "must be a commit SHA (4 to 40 hex characters)")
    return value


class RepoRef(_Model):
    path: str
    base: str
    solution: str | None = None

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise PydanticCustomError("absolute_path", "must be an absolute local path")
        return value

    @field_validator("base", "solution", mode="before")
    @classmethod
    def _commit(cls, value: Any) -> Any:
        return _sha(value)


class SetupStep(_Model):
    argv: list[NonEmpty] = Field(min_length=1)
    timeout_s: int = Field(default=600, ge=1, le=3600)

    @field_validator("argv", mode="before")
    @classmethod
    def _not_a_shell_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            raise PydanticCustomError(
                "shell_string", "argv must be a list of arguments; shell strings are not supported"
            )
        return value


class HiddenTest(_Model):
    path: str  # relative to the repository root
    source: str  # relative to the evals directory

    @field_validator("path", "source")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return _relative(value)


class EvalTask(_Model):
    id: str
    category: Category
    expected_risk: Risk
    repo: RepoRef
    request: str = Field(min_length=REQUEST_MIN_CHARS, max_length=REQUEST_MAX_CHARS)
    setup: list[SetupStep] = Field(default_factory=list)
    done_criteria: list[Criterion] = Field(min_length=1)
    hidden_tests: list[HiddenTest] = Field(default_factory=list)
    reference_diff: str | None = None
    context_files: list[str] = Field(default_factory=list)
    tags: list[NonEmpty] = Field(default_factory=list)
    notes: str | None = None  # never sent to a model

    @field_validator("id")
    @classmethod
    def _task_id(cls, value: str) -> str:
        if TASK_ID.fullmatch(value) is None:
            raise PydanticCustomError(
                "task_id", "must match ^[a-z0-9][a-z0-9-]{2,63}$ (lowercase, digits, '-')"
            )
        return value

    @field_validator("request")
    @classmethod
    def _filled_in(cls, value: str) -> str:
        if value.strip().startswith("TODO"):
            raise PydanticCustomError("todo", "still a TODO: describe the task as you would ask it")
        return value

    @field_validator("reference_diff")
    @classmethod
    def _diff_path(cls, value: str | None) -> str | None:
        return None if value is None else _relative(value)

    @field_validator("context_files")
    @classmethod
    def _context_paths(cls, value: list[str]) -> list[str]:
        return [_relative(path) for path in value]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.repo.solution is None and self.reference_diff is None:
            raise PydanticCustomError("no_solution", "give repo.solution or reference_diff")
        paths = [test.path for test in self.hidden_tests]
        if len(set(paths)) != len(paths):
            raise PydanticCustomError("duplicate_hidden", "hidden_tests repeat a path")
        leaked = sorted(set(paths) & set(self.context_files))
        if leaked:
            raise PydanticCustomError(
                "hidden_in_context",
                "hidden test paths must not be context_files: {paths}",
                {"paths": ", ".join(leaked)},
            )
        return self

    @property
    def criteria(self) -> list[CommandCriterion | FileExistsCriterion]:
        return list(self.done_criteria)


def substitute(data: Any, variables: Mapping[str, str]) -> Any:
    """``data`` with ``${NAME}`` in every string replaced. Unknown names raise KeyError."""
    if isinstance(data, str):
        return _PLACEHOLDER.sub(lambda match: variables[match.group(1)], data)
    if isinstance(data, list):
        return [substitute(item, variables) for item in data]
    if isinstance(data, dict):
        return {key: substitute(value, variables) for key, value in data.items()}
    return data


def _issues(error: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in issue['loc']) or 'task'}: {issue['msg']}"
        for issue in error.errors(include_input=False)
    ]


def inside(root: Path, relative: str) -> Path | None:
    """``root / relative`` resolved, or None when it leaves ``root``."""
    target = (root / relative).resolve()
    return target if target.is_relative_to(root.resolve()) else None


def load_task(
    file: Path, evals_root: Path, variables: Mapping[str, str] | None = None
) -> EvalTask:
    """Read, validate and check one task file. Raises TaskError with every problem."""
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise TaskError(file, [f"cannot read: {error}"]) from None
    if not isinstance(data, dict):
        raise TaskError(file, ["must be a YAML mapping"])
    if variables is not None:
        try:
            data = substitute(data, variables)
        except KeyError as error:
            raise TaskError(file, [f"unknown placeholder ${{{error.args[0]}}}"]) from None
    try:
        task = EvalTask.model_validate(data)
    except ValidationError as error:
        raise TaskError(file, _issues(error)) from None

    problems = []
    if task.id != file.stem:
        problems.append(f"id: '{task.id}' must equal the file name '{file.stem}'")
    referenced = [(f"hidden_tests.{n}.source", t.source) for n, t in enumerate(task.hidden_tests)]
    if task.reference_diff is not None:
        referenced.append(("reference_diff", task.reference_diff))
    for field, relative in referenced:
        target = inside(evals_root, relative)
        if target is None:
            problems.append(f"{field}: resolves outside the evals directory")
        elif not target.is_file():
            problems.append(f"{field}: file not found: {relative}")
    if not Path(task.repo.path, ".git").exists():
        problems.append(f"repo.path: not a git repository: {task.repo.path}")
    if problems:
        raise TaskError(file, problems)
    return task

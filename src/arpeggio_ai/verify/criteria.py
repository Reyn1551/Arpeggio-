"""Done criteria specs, stored as JSON in ``done_criteria.spec`` (VER-01).

Two kinds exist in M0.4:

- ``command``: run ``argv`` in the attempt worktree and compare the exit code.
  ``{"kind": "command", "argv": ["uv", "run", "pytest", "tests/test_x.py"],
  "expect_exit": 0, "timeout_s": 300}``
- ``file_exists``: a path inside the worktree must exist.
  ``{"kind": "file_exists", "path": "src/pkg/new_module.py"}``

``argv`` is a list of arguments, never a shell string, so a check runs the same way on every
platform. ``metric`` and ``review`` criteria come later.
"""

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic_core import PydanticCustomError

NonEmpty = Annotated[str, StringConstraints(min_length=1)]
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CommandCriterion(_Spec):
    kind: Literal["command"]
    argv: list[NonEmpty] = Field(min_length=1)
    expect_exit: int = 0
    timeout_s: int = Field(default=300, ge=1, le=3600)

    @field_validator("argv", mode="before")
    @classmethod
    def _not_a_shell_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            raise PydanticCustomError(
                "shell_string",
                "argv must be a list of arguments; shell strings are not supported",
            )
        return value


def relative_path_problem(path: str) -> str | None:
    """Why ``path`` is not a safe relative POSIX path inside a worktree, or None."""
    if not path or path.strip() != path:
        return "must be a non-empty path without surrounding spaces"
    if "\\" in path:
        return "must use forward slashes"
    if path.startswith("/") or _WINDOWS_DRIVE.match(path):
        return "must be relative to the repository root"
    if any(part in ("", ".", "..") for part in path.split("/")):
        return "must not contain empty, '.' or '..' segments"
    return None


class FileExistsCriterion(_Spec):
    kind: Literal["file_exists"]
    path: str

    @field_validator("path")
    @classmethod
    def _relative(cls, value: str) -> str:
        problem = relative_path_problem(value)
        if problem is not None:
            raise PydanticCustomError("relative_path", problem)
        return value


Criterion = Annotated[CommandCriterion | FileExistsCriterion, Field(discriminator="kind")]
_ADAPTER: TypeAdapter[CommandCriterion | FileExistsCriterion] = TypeAdapter(Criterion)


class CriterionError(ValueError):
    """A criterion spec failed validation. The message lists every problem."""


def parse_criterion(data: object) -> CommandCriterion | FileExistsCriterion:
    try:
        return _ADAPTER.validate_python(data)
    except ValidationError as error:
        problems = [
            f"{'.'.join(str(part) for part in issue['loc']) or 'spec'}: {issue['msg']}"
            for issue in error.errors(include_input=False)
        ]
        raise CriterionError("; ".join(problems)) from None


def describe(criterion: CommandCriterion | FileExistsCriterion) -> str:
    """One line for the model prompt."""
    if isinstance(criterion, CommandCriterion):
        command = " ".join(criterion.argv)
        return f"The command `{command}` exits with code {criterion.expect_exit}."
    return f"The file `{criterion.path}` exists."

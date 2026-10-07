from typing import Any

import pytest

from arpeggio_ai.verify.criteria import (
    CommandCriterion,
    CriterionError,
    FileExistsCriterion,
    describe,
    parse_criterion,
    relative_path_problem,
)


def test_command_defaults() -> None:
    criterion = parse_criterion({"kind": "command", "argv": ["pytest", "tests/test_x.py"]})
    assert criterion == CommandCriterion(
        kind="command", argv=["pytest", "tests/test_x.py"], expect_exit=0, timeout_s=300
    )


def test_file_exists() -> None:
    criterion = parse_criterion({"kind": "file_exists", "path": "src/pkg/new_module.py"})
    assert criterion == FileExistsCriterion(kind="file_exists", path="src/pkg/new_module.py")


def test_shell_string_is_rejected() -> None:
    with pytest.raises(CriterionError) as caught:
        parse_criterion({"kind": "command", "argv": "uv run pytest -q"})
    assert str(caught.value) == (
        "command.argv: argv must be a list of arguments; shell strings are not supported"
    )


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        ({"kind": "command", "argv": []}, "command.argv"),
        ({"kind": "command", "argv": ["pytest", ""]}, "command.argv.1"),
        ({"kind": "command", "argv": ["pytest"], "timeout_s": 0}, "command.timeout_s"),
        ({"kind": "command", "argv": ["pytest"], "timeout_s": 3601}, "command.timeout_s"),
        ({"kind": "command", "argv": ["pytest"], "expect_exit": "0"}, "command.expect_exit"),
        ({"kind": "command", "argv": ["pytest"], "shell": True}, "command.shell"),
    ],
)
def test_invalid_command_fields(spec: dict[str, Any], field: str) -> None:
    with pytest.raises(CriterionError, match=f"^{field}: "):
        parse_criterion(spec)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "C:/Windows/x",
        "c:x",
        "../outside.py",
        "src/../../x",
        "src\\pkg\\x.py",
        "",
        "src//x.py",
        "./x.py",
        " x.py",
    ],
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    with pytest.raises(CriterionError, match=r"^file_exists.path: "):
        parse_criterion({"kind": "file_exists", "path": path})


def test_relative_path_problem_accepts_plain_relative_paths() -> None:
    assert relative_path_problem("src/pkg/x.py") is None
    assert relative_path_problem(".github/workflows/ci.yml") is None


@pytest.mark.parametrize("kind", ["metric", "review", "nope"])
def test_later_kinds_are_not_supported_yet(kind: str) -> None:
    with pytest.raises(CriterionError, match="kind"):
        parse_criterion({"kind": kind})


def test_non_dict_spec_is_rejected() -> None:
    with pytest.raises(CriterionError):
        parse_criterion(["pytest"])


def test_describe() -> None:
    assert describe(parse_criterion({"kind": "command", "argv": ["pytest", "-q"]})) == (
        "The command `pytest -q` exits with code 0."
    )
    assert describe(parse_criterion({"kind": "file_exists", "path": "a/b.py"})) == (
        "The file `a/b.py` exists."
    )

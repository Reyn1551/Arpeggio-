"""Escalation failure report (VER-03): contents and the hidden-test output rule."""

import pytest

from arpeggio_ai.evals.budget import FEEDBACK_BASE_CHARS
from arpeggio_ai.evals.feedback import (
    FEEDBACK_TAIL_CHARS,
    TRUNCATED_HINT,
    FailedCheck,
    failure_report,
    output_may_show_hidden,
)

HIDDEN = ["tests/test_mul.py"]
PY = "/usr/bin/python"


@pytest.mark.parametrize(
    ("argv", "withheld"),
    [
        ([PY, "-m", "pytest", "tests/test_mul.py"], True),  # names the hidden path
        ([PY, "-m", "pytest", "TESTS\\Test_Mul.py"], True),  # case and separators
        ([PY, "-m", "unittest", "discover", "-s", "tests"], True),  # parent directory
        ([PY, "-m", "pytest", "."], True),
        ([PY, "-m", "unittest", "tests.test_mul"], True),  # module form
        ([PY, "-m", "pytest", "--rootdir=tests"], True),
        ([PY, "-m", "pytest"], True),  # no explicit path: may discover hidden tests
        (["npm", "test"], True),
        ([PY, "-m", "pytest", "tests/test_calc.py"], False),
        ([PY, "-m", "unittest", "tests.test_calc"], False),
        (None, False),  # file_exists
    ],
)
def test_output_may_show_hidden(argv: list[str] | None, withheld: bool) -> None:
    assert output_may_show_hidden(argv, "1 failed", HIDDEN) is withheld


def test_output_naming_a_hidden_file_is_withheld() -> None:
    argv = [PY, "-m", "pytest", "tests/test_calc.py"]
    assert output_may_show_hidden(argv, "error in tests/TEST_MUL.py line 3", HIDDEN)


def test_without_hidden_tests_output_is_always_shown() -> None:
    assert not output_may_show_hidden([PY, "-m", "pytest"], "x", [])


def test_report_lists_failures_with_exit_codes_and_tails() -> None:
    long_output = "a" * 3000 + "THE END"
    report = failure_report(
        1,
        "checks_failed",
        [
            FailedCheck(1, 1, [PY, "-m", "pytest", "tests/test_calc.py"], long_output),
            FailedCheck(3, 1, [PY, "-m", "pytest", "tests/test_mul.py"], "SECRET-MARKER"),
            FailedCheck(4, None, [PY, "x/y.py"], "timed out"),
        ],
        HIDDEN,
    )
    assert report.startswith("Attempt 1 failed: checks_failed.")
    assert "- check 1: exit code 1" in report
    assert "- check 3: exit code 1\n  output withheld" in report
    assert "- check 4: exit code none (did not finish)" in report
    assert "SECRET-MARKER" not in report
    assert "THE END" in report
    tail = report.split("````\n", 1)[1].split("\n````", 1)[0]
    assert len(tail) == FEEDBACK_TAIL_CHARS


def test_patch_failure_report_has_no_checks() -> None:
    report = failure_report(2, "patch_does_not_apply", [], HIDDEN)
    assert report.splitlines()[0] == "Attempt 2 failed: patch_does_not_apply."
    assert "check" not in report.split("\n", 1)[1].replace("checks", "")


def test_output_truncated_report_asks_for_short_reasoning() -> None:
    report = failure_report(1, "output_truncated", [], HIDDEN)
    assert report.splitlines()[:2] == ["Attempt 1 failed: output_truncated.", TRUNCATED_HINT]
    assert len(report) <= FEEDBACK_BASE_CHARS


def test_other_kinds_get_no_truncation_hint() -> None:
    for kind in ("patch_missing", "checks_failed"):
        assert TRUNCATED_HINT not in failure_report(1, kind, [], HIDDEN)

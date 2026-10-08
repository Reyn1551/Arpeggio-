"""`eval new --check-file --check-pattern`: a generated Node hidden test (M0.6 §4.9)."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from evalkit import ENV, Suite
from typer.testing import CliRunner

from arpeggio_ai.cli.app import app
from arpeggio_ai.evals.scaffold import NewTask, ScaffoldError, scaffold
from arpeggio_ai.evals.selfcheck import CheckRun
from arpeggio_ai.evals.suite import discover
from arpeggio_ai.evals.textcheck import TextCheck, render
from arpeggio_ai.paths import EVALS_ENV_VAR
from arpeggio_ai.safety.secret_scan import SecretScanner

NODE = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
HIDDEN = "tests/arpeggio-hidden/docs-usage.test.mjs"


@pytest.fixture
def suite(tmp_path: Path, home: Path) -> Suite:
    s = Suite(tmp_path, home)
    s.root = tmp_path / "my-evals"
    return s


def new(suite: Suite, check: TextCheck, base: str = "c2", solution: str = "c3") -> NewTask:
    return asyncio.run(
        scaffold(
            suite.repo,
            suite.sha(base),
            suite.sha(solution),
            "docs-usage",
            suite.root,
            home=suite.home,
            env=ENV,
            scanner=SecretScanner(),
            request="Document how to use add and mul in docs/usage.md.",
            category="docs",
            risk="low",
            text_check=check,
        )
    )


def test_generates_hidden_test_and_criterion(suite: Suite) -> None:
    result = new(suite, TextCheck("docs/usage.md", r"calc\.ops\.mul\(a, b\)", "usage names mul"))
    assert result.hidden_tests == [HIDDEN]
    (entry,) = discover(suite.root)
    task = entry.task
    assert task is not None, entry.problems
    assert (task.category, task.expected_risk) == ("docs", "low")
    assert task.request.startswith("Document how")
    assert [t.path for t in task.hidden_tests] == [HIDDEN]
    assert [c.model_dump()["argv"] for c in task.done_criteria] == [["node", "--test", HIDDEN]]
    data = (suite.root / "fixtures/docs-usage/hidden" / HIDDEN).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    data.decode("ascii")
    assert b"\r\n" not in data
    assert b'new RegExp("calc\\\\.ops\\\\.mul\\\\(a, b\\\\)")' in data
    assert not suite.root.joinpath("tasks/docs-usage.yaml").read_bytes().startswith(b"\xef\xbb\xbf")


@NODE
def test_generated_test_passes_eval_check(suite: Suite) -> None:
    new(suite, TextCheck("docs/usage.md", r"calc\.ops\.mul"))
    (entry,) = discover(suite.root)
    run = CheckRun(suite.root, suite.home, suite.artifacts, lambda _t: ENV)
    check = asyncio.run(run.check(entry, 0))
    assert check.status == "valid", check.detail
    assert check.base is not None and check.base.failing == [1]


def test_pattern_matching_at_base_is_refused(suite: Suite) -> None:
    with pytest.raises(ScaffoldError, match=r"already matches calc.toml at base"):
        new(suite, TextCheck("calc.toml", r"precision = \d"))
    assert not (suite.root / "tasks/docs-usage.yaml").exists()


def test_pattern_missing_at_solution_is_refused(suite: Suite) -> None:
    with pytest.raises(ScaffoldError, match=r"does not match docs/usage.md at the solution"):
        new(suite, TextCheck("docs/usage.md", r"calc\.ops\.div"))
    with pytest.raises(ScaffoldError, match="does not exist at the solution"):
        new(suite, TextCheck("docs/nope.md", r"x"))
    with pytest.raises(ScaffoldError, match="not a valid regular expression"):
        new(suite, TextCheck("docs/usage.md", r"calc("))


def test_render_is_ascii_and_escapes_strings() -> None:
    data = render("t-one", TextCheck("docs/café.md", 'say "hi"\\d', "café test"))
    text = data.decode("ascii")
    assert 'const FILE = "docs/caf\\u00e9.md";' in text
    assert 'new RegExp("say \\"hi\\"\\\\d")' in text
    assert 'test("caf\\u00e9 test"' in text


def test_cli_needs_file_and_pattern_together(suite: Suite, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EVALS_ENV_VAR, str(suite.root))
    args = ["eval", "new", "--repo", str(suite.repo), "--base", suite.sha("c2")]
    args += ["--solution", suite.sha("c3"), "--id", "docs-usage", "--json"]
    result = CliRunner().invoke(app, [*args, "--check-file", "docs/usage.md"])
    assert result.exit_code == 2
    assert "together" in json.loads(result.stdout)["errors"][0]["message"]
    result = CliRunner().invoke(
        app,
        [
            *args,
            "--check-file",
            "docs/usage.md",
            "--check-pattern",
            r"calc\.ops\.mul",
            "--request",
            "Document add and mul usage.",
            "--category",
            "docs",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["hidden_tests"] == [HIDDEN]

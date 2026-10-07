import json
from importlib.metadata import version

import pytest
from typer.testing import CliRunner

import arpeggio_ai
from arpeggio_ai.cli.app import app

runner = CliRunner()


# App-level options


def test_version_matches_package_metadata() -> None:
    assert arpeggio_ai.__version__ == version("arpeggio-ai") == "0.0.1"


def test_version_human() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == "arpeggio 0.0.1\n"


@pytest.mark.parametrize("args", [["--version", "--json"], ["--json", "--version"]])
def test_version_json(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"version": "0.0.1"}
    assert result.stderr == ""


@pytest.mark.parametrize("args", [[], ["--help"], ["--json"]])
def test_root_help(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert "--version" in result.stdout
    assert "--json" in result.stdout

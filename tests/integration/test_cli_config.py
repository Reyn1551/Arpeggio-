import json
import re
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

import arpeggio_ai
from arpeggio_ai.cli.app import app
from arpeggio_ai.cli.commands import config as config_commands
from arpeggio_ai.config.models import INLINE_KEY_MESSAGE, parse_config

WriteFile = Callable[[str], Path]

runner = CliRunner()


def invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


# App-level options


def test_version_matches_package_metadata() -> None:
    assert arpeggio_ai.__version__ == version("arpeggio-ai") == "0.0.1"


def test_version_human() -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert result.stdout == "arpeggio 0.0.1\n"


@pytest.mark.parametrize("args", [["--version", "--json"], ["--json", "--version"]])
def test_version_json(args: list[str]) -> None:
    result = invoke(*args)
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"version": "0.0.1"}
    assert result.stderr == ""


@pytest.mark.parametrize("args", [[], ["--help"], ["--json"]])
def test_root_help(args: list[str]) -> None:
    result = invoke(*args)
    assert result.exit_code == 0
    assert "--version" in result.stdout
    assert "--json" in result.stdout


# config validate (cases 15, 16)


def test_validate_ok_human(write_global: WriteFile, template_text: str, home: Path) -> None:
    write_global(template_text)
    result = invoke("config", "validate")
    assert result.exit_code == 0
    assert result.stdout.splitlines()[:2] == [
        "Config OK",
        "profile standard, 2 providers, 3 models, tiers 1, 2, 3",
    ]
    assert str(home / "config.toml") in result.stdout
    assert result.stderr == ""


def test_validate_ok_json(write_global: WriteFile, template_text: str) -> None:
    write_global(template_text)
    result = invoke("config", "validate", "--json")
    assert result.exit_code == 0
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout) == {
        "ok": True,
        "profile": "standard",
        "providers": 2,
        "models": 3,
        "tiers": [1, 2, 3],
    }
    assert result.stderr == ""


def test_validate_reports_only_tiers_present(write_global: WriteFile, template_text: str) -> None:
    without_tier2 = re.sub(r'\[models\."tier2\.mid"\]\n(?:.+\n)+\n', "", template_text)
    assert "tier2.mid" not in without_tier2
    write_global(without_tier2)
    result = invoke("config", "validate", "--json")
    assert json.loads(result.stdout)["tiers"] == [1, 3]


def test_validate_singular_counts(write_global: WriteFile, template_text: str) -> None:
    text = re.sub(r"\[providers\.deepseek\]\n(?:.+\n)+\n", "", template_text)
    text = re.sub(r'\[models\."tier1\.cheap"\]\n(?:.+\n)+\n', "", text)
    text = re.sub(r'\[models\."tier2\.mid"\]\n(?:.+\n)+\n', "", text)
    write_global(text)
    result = invoke("config", "validate")
    assert result.exit_code == 0, result.stderr
    assert "1 provider, 1 model, tiers 3" in result.stdout


def test_validate_invalid_human(write_global: WriteFile, template_text: str) -> None:
    write_global(template_text.replace('effort = "high"', 'effort = "low"'))
    result = invoke("config", "validate")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert "defaults.counterfactual_route.effort" in result.stderr


def test_validate_invalid_json(write_global: WriteFile, template_text: str) -> None:
    path = write_global(template_text.replace("per_day_usd    = 10.00", "per_day_usd = 1.0"))
    result = invoke("config", "validate", "--json")
    assert result.exit_code == 2
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "ok": False,
        "errors": [
            {
                "file": str(path),
                "field": "budget.per_day_usd",
                "message": "must be >= per_task_usd",
            }
        ],
    }


@pytest.mark.parametrize("command", ["validate", "show"])
def test_missing_global_config_exits_2_and_points_to_init(command: str) -> None:
    human = invoke("config", command)
    assert human.exit_code == 2
    assert "arpeggio init" in human.stderr

    as_json = invoke("config", command, "--json")
    assert as_json.exit_code == 2
    payload = json.loads(as_json.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["field"] is None
    assert "arpeggio init" in payload["errors"][0]["message"]


@pytest.mark.parametrize("as_json", [True, False])
def test_inline_api_key_never_reaches_output(
    write_global: WriteFile, template_text: str, as_json: bool
) -> None:
    write_global(template_text.replace("env:DEEPSEEK_API_KEY", "sk-test-123"))
    result = invoke("config", "validate", *(["--json"] if as_json else []))
    assert result.exit_code == 2
    assert "sk-test-123" not in result.stdout
    assert "sk-test-123" not in result.stderr
    assert INLINE_KEY_MESSAGE in (result.stdout if as_json else result.stderr)


def test_human_errors_print_brackets_literally(write_global: WriteFile, template_text: str) -> None:
    write_global(template_text + '\n[repo]\nprivacy_class = "public"\n')
    result = invoke("config", "validate")
    assert result.exit_code == 2
    assert "[repo] is only allowed in <repo>/.arpeggio/config.toml" in result.stderr


def test_validate_with_repo_merges_and_names_repo_file(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text)
    repo_path = write_repo(
        '[providers.local]\nkind = "openai_compatible"\n'
        'base_url = "https://llm.example.test"\napi_key = "env:LOCAL_KEY"\n'
    )
    human = invoke("config", "validate", "--repo", str(repo_dir))
    assert human.exit_code == 0
    assert "3 providers" in human.stdout
    assert str(repo_path) in human.stdout

    as_json = invoke("config", "validate", "--repo", str(repo_dir), "--json")
    assert json.loads(as_json.stdout)["providers"] == 3


def test_validate_with_repo_without_config_says_global_only(
    write_global: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text)
    result = invoke("config", "validate", "--repo", str(repo_dir))
    assert result.exit_code == 0
    assert "not found, global only" in result.stdout


def test_validate_with_missing_repo_dir_exits_2(
    write_global: WriteFile, template_text: str, tmp_path: Path
) -> None:
    write_global(template_text)
    result = invoke("config", "validate", "--repo", str(tmp_path / "nope"), "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["errors"][0]["message"] == "repo directory not found"


def test_root_json_flag_applies_to_config(write_global: WriteFile, template_text: str) -> None:
    write_global(template_text)
    result = invoke("--json", "config", "validate")
    assert json.loads(result.stdout)["ok"] is True


# config show


def test_show_json_is_the_effective_config(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text)
    write_repo('[budget]\nper_task_usd = 1.5\n[repo]\nprivacy_class = "client"\n')

    result = invoke("config", "show", "--repo", str(repo_dir), "--json")

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert set(payload) == {"ok", "config"}
    assert payload["ok"] is True
    config = payload["config"]
    assert config["budget"]["per_task_usd"] == 1.5
    assert config["providers"]["anthropic"] == {
        "kind": "anthropic",
        "api_key": "env:ANTHROPIC_API_KEY",
        "gateway": False,
        "data_use": "unknown",
        "prompt_overhead_tokens": 0,
    }
    assert config["repo"] == {
        "privacy_class": "client",
        "check_env": [],
        "secret_scan_allow": [],
    }
    assert config["privacy"] == {"allow_training_providers": False}
    assert parse_config(config).budget.per_task_usd == 1.5


def test_show_human_prints_a_tree(write_global: WriteFile, template_text: str) -> None:
    write_global(template_text)
    result = invoke("config", "show")
    assert result.exit_code == 0
    for expected in (
        "budget",
        "per_task_usd = 2.0",
        "tier2.mid",
        'efforts = ["low", "medium", "high"]',
        'api_key = "env:ANTHROPIC_API_KEY"',
    ):
        assert expected in result.stdout


def test_show_invalid_toml_exits_2(write_global: WriteFile) -> None:
    write_global("[budget\n")
    result = invoke("config", "show", "--json")
    assert result.exit_code == 2
    assert "line 1" in json.loads(result.stdout)["errors"][0]["message"]


# Unexpected errors and help


@pytest.mark.parametrize("command", ["validate", "show"])
def test_unexpected_error_exits_1(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    def boom(repo: Path | None = None) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(config_commands, "load_config", boom)

    human = invoke("config", command)
    assert human.exit_code == 1
    assert human.stderr == "error: RuntimeError: boom\n"

    as_json = invoke("config", command, "--json")
    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout) == {
        "ok": False,
        "errors": [{"file": None, "field": None, "message": "RuntimeError: boom"}],
    }


@pytest.mark.parametrize(
    "args",
    [
        ["config"],
        ["config", "--help"],
        ["config", "validate", "--help"],
        ["config", "show", "--help"],
    ],
)
def test_config_help(args: list[str]) -> None:
    result = invoke(*args)
    assert result.exit_code == 0
    assert "Usage" in result.stdout

"""`arpeggio eval new|check|list` through the CLI (CLI-01, CLI-03, EVL-01, EVL-02)."""

import json
from pathlib import Path

import pytest
from samplerepo import materialize, sample_suite
from typer.testing import CliRunner, Result

from arpeggio_ai.cli.app import app
from arpeggio_ai.paths import EVALS_ENV_VAR

runner = CliRunner()


def invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


@pytest.fixture
def sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, dict[str, str]]:
    repo, root, variables = sample_suite(tmp_path)
    materialize(root, variables)
    monkeypatch.setenv(EVALS_ENV_VAR, str(root))
    return repo, root, variables


def test_help_lists_eval_commands() -> None:
    result = invoke("eval", "--help")
    assert result.exit_code == 0
    for name in ("new", "check", "list"):
        assert name in result.stdout
    assert invoke("eval").exit_code == 0


def test_check_json_all_valid(sample: tuple[Path, Path, dict[str, str]]) -> None:
    _, root, _ = sample
    result = invoke("eval", "check", "--json")
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert {t["id"]: t["status"] for t in data["tasks"]} == {
        "sample-fix-add": "valid",
        "sample-feature-mul": "valid",
        "sample-docs-config": "valid",
    }
    assert Path(data["report"]).parent == root / ".reports"


def test_check_selects_by_split_and_id(sample: tuple[Path, Path, dict[str, str]]) -> None:
    result = invoke("--json", "eval", "check", "--split", "holdout")
    assert [t["id"] for t in json.loads(result.stdout)["tasks"]] == ["sample-docs-config"]
    result = invoke("eval", "check", "--id", "sample-fix-add", "--json")
    assert [t["id"] for t in json.loads(result.stdout)["tasks"]] == ["sample-fix-add"]


def test_check_unknown_id_is_an_error(sample: tuple[Path, Path, dict[str, str]]) -> None:
    result = invoke("eval", "check", "--id", "nope-task", "--json")
    assert result.exit_code == 1
    assert "unknown task ids: nope-task" in json.loads(result.stdout)["errors"][0]["message"]


def test_check_exit_code_is_1_when_a_task_is_not_valid(
    sample: tuple[Path, Path, dict[str, str]],
) -> None:
    _, root, _ = sample
    file = root / "tasks" / "sample-fix-add.yaml"
    file.write_text(file.read_text(encoding="utf-8").replace("timeout_s: 120", "timeout_s: 0"))
    result = invoke("eval", "check", "--id", "sample-fix-add")
    assert result.exit_code == 1
    assert "0/1 valid" in result.stdout
    data = json.loads(invoke("eval", "check", "--id", "sample-fix-add", "--json").stdout)
    assert data["ok"] is False
    assert data["tasks"][0]["status"] == "invalid_schema"


def test_list_shows_status_and_warnings(sample: tuple[Path, Path, dict[str, str]]) -> None:
    before = json.loads(invoke("eval", "list", "--json").stdout)
    assert {t["status"] for t in before["tasks"]} == {"unchecked"}
    assert invoke("eval", "check", "--id", "sample-fix-add").exit_code == 0
    data = json.loads(invoke("eval", "list", "--json").stdout)
    rows = {t["id"]: t for t in data["tasks"]}
    assert rows["sample-fix-add"]["status"] == "valid"
    assert rows["sample-fix-add"]["category"] == "bugfix"
    assert rows["sample-docs-config"]["split"] == "holdout"
    summary = data["summary"]
    assert (summary["tuning"], summary["holdout"], summary["total"]) == (2, 1, 3)
    assert summary["holdout_share"] == pytest.approx(0.3333, abs=1e-4)
    assert summary["warnings"] == ["only 3 tasks; EVL-01 asks for at least 20"]

    human = invoke("eval", "list")
    assert human.exit_code == 0
    assert "holdout share 33%" in human.stdout
    assert "warning: only 3 tasks" in human.stdout


def test_list_warns_about_low_holdout(sample: tuple[Path, Path, dict[str, str]]) -> None:
    _, root, _ = sample
    (root / "holdout" / "sample-docs-config.yaml").unlink()
    data = json.loads(invoke("eval", "list", "--split", "tuning", "--json").stdout)
    assert "holdout share 0% is below the 30% EVL-02 asks for" in data["summary"]["warnings"]


def test_new_then_list(sample: tuple[Path, Path, dict[str, str]]) -> None:
    repo, _, variables = sample
    args = ["eval", "new", "--repo", str(repo), "--id", "calc-mul-copy", "--holdout"]
    args += ["--base", variables["SAMPLE_C1"][:8], "--solution", variables["SAMPLE_C2"]]
    result = invoke(*args, "--json")
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["split"] == "holdout"
    assert data["hidden_tests"] == ["tests/test_mul.py"]
    assert data["scan"] == {"files": 3, "findings_by_type": {}}
    again = invoke(*args)
    assert again.exit_code == 1
    assert "already exists" in again.stderr
    human = invoke(*args, "--force", "--tests-glob", "src/**")
    assert human.exit_code == 0
    assert "src/calc/ops.py" in human.stdout
    rows = json.loads(invoke("eval", "list", "--json").stdout)["tasks"]
    assert {"id": "calc-mul-copy", "status": "invalid_schema"}.items() <= next(
        r for r in rows if r["id"] == "calc-mul-copy"
    ).items()


def test_evals_dir_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: Path, template_text: str
) -> None:
    target = tmp_path / "configured-evals"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        template_text + f'\n[evals]\ndir = "{target.as_posix()}"\n', encoding="utf-8"
    )
    monkeypatch.delenv(EVALS_ENV_VAR, raising=False)
    data = json.loads(invoke("eval", "list", "--json").stdout)
    assert Path(data["evals_dir"]) == target
    assert data["tasks"] == []

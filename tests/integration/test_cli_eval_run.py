"""`arpeggio eval run|runs|report` through the CLI with a fake provider (EVL-03, EVL-06)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import FAKE_KEY, MONDAY_OFFPEAK, MONDAY_PEAK, deepseek_usage, ok
from runkit import FIX_ADD, mul_patch, write_report
from samplerepo import materialize, sample_suite
from typer.testing import CliRunner, Result

from arpeggio_ai.cli.app import app
from arpeggio_ai.cli.commands import evals as evals_cli
from arpeggio_ai.config.loader import template_bytes
from arpeggio_ai.paths import EVALS_ENV_VAR

runner = CliRunner()
OPT_IN = "\n[privacy]\nallow_training_providers = true\n"


def template(name: str) -> str:
    return template_bytes(name).decode("utf-8").replace("\r\n", "\n")


def invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


def data(result: Result) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


class Model:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        reply = mul_patch("a * b") if "product" in user else FIX_ADD
        return ok(reply, usage=deepseek_usage(800, 0, 100), model=body["model"])


@pytest.fixture
def suite(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    _, root, variables = sample_suite(tmp_path)
    materialize(root, variables)
    monkeypatch.setenv(EVALS_ENV_VAR, str(root))
    home.mkdir(parents=True, exist_ok=True)
    text = template("micro-deepseek") + OPT_IN
    (home / "config.toml").write_bytes(text.encode("utf-8"))
    write_report(root, dict.fromkeys(["sample-fix-add", "sample-feature-mul"], "valid"))
    monkeypatch.setattr(evals_cli, "now_utc", lambda: MONDAY_OFFPEAK)
    yield root


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> Model:
    fake = Model()
    monkeypatch.setattr(evals_cli, "TRANSPORT", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    return fake


def test_help_lists_new_commands() -> None:
    result = invoke("eval", "--help")
    for name in ("run", "report", "runs"):
        assert name in result.stdout


def test_dry_run_needs_no_keys_and_calls_nothing(
    suite: Path, model: Model, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    result = invoke("eval", "run", "--split", "tuning", "--dry-run", "--repeats", "3", "--json")
    assert result.exit_code == 0, result.stdout
    out = data(result)
    assert out["dry_run"] is True and out["profile"] == "micro"
    assert out["plan"]["planned_task_runs"] == 6
    assert out["estimate_usd"] > 0
    assert out["limit_usd"] == 0.5  # eval_per_month_usd from the template
    assert model.requests == []
    assert not (home / "worktrees").exists() or list((home / "worktrees").iterdir()) == []
    text = invoke("eval", "run", "--split", "tuning", "--dry-run")
    assert "tier1.flash@low -> tier2.flash@medium -> tier3.pro@high" in text.stdout
    assert "Dry run: no model was called." in text.stdout


def test_dry_run_lists_stale_and_unchecked_tasks(suite: Path) -> None:
    out = data(invoke("eval", "run", "--split", "all", "--dry-run", "--json"))
    skipped = {s["id"]: s["reason"] for s in out["plan"]["skipped"]}
    assert skipped == {"sample-docs-config": "unchecked"}


def test_real_run_needs_a_budget(suite: Path, model: Model, tmp_path: Path) -> None:
    text = template("micro-deepseek").replace("eval_per_month_usd  = 0.50", "")
    other = tmp_path / "no-eval-budget.toml"
    other.write_bytes((text + OPT_IN).encode("utf-8"))
    result = invoke("eval", "run", "--split", "tuning", "--config", str(other), "--json")
    assert result.exit_code == 1
    assert "no eval budget" in data(result)["errors"][0]["message"]
    assert model.requests == []


def test_real_run_refuses_an_estimate_over_the_cap(suite: Path, model: Model) -> None:
    result = invoke("eval", "run", "--split", "tuning", "--max-usd", "0.000001", "--json")
    assert result.exit_code == 1
    assert "worst-case estimate" in data(result)["errors"][0]["message"]
    assert model.requests == []


def test_peak_prices_refuse_without_allow_peak(
    suite: Path, model: Model, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evals_cli, "now_utc", lambda: MONDAY_PEAK)
    args = ["eval", "run", "--split", "tuning", "--strategy", "middle", "--max-usd", "0.4"]
    result = invoke(*args, "--json")
    assert result.exit_code == 1
    message = data(result)["errors"][0]["message"]
    assert "deepseek is at peak prices until 2026-10-05 04:00 UTC" in message
    assert "--allow-peak" in message
    assert model.requests == []
    result = invoke(*args, "--allow-peak", "--json")
    assert result.exit_code == 0, result.stdout
    assert data(result)["status"] == "completed"


def test_run_then_runs_and_report(suite: Path, model: Model) -> None:
    result = invoke(
        "eval", "run", "--split", "tuning", "--strategy", "middle", "--max-usd", "0.4", "--json"
    )
    assert result.exit_code == 0, result.stdout
    out = data(result)
    assert out["status"] == "completed" and out["reason"] is None
    assert 0 < out["spent_usd"] <= 0.4
    assert set(out["runs"]) == {"middle"}
    assert len(model.requests) == 2

    runs = data(invoke("eval", "runs", "--json"))["runs"]
    assert [(r["strategy"], r["profile"], r["split"], r["status"]) for r in runs] == [
        ("middle", "micro", "tuning", "completed")
    ]
    assert runs[0]["solved"] == 2 and runs[0]["task_runs"] == 2

    report = invoke("eval", "report", "--json")
    assert report.exit_code == 0, report.stdout
    rep = data(report)
    markdown = Path(rep["markdown"])
    assert markdown.parent == suite / ".reports" and markdown.name.startswith("baseline-")
    assert "## Profile `micro`, split `tuning`" in markdown.read_text("utf-8")
    (section,) = rep["sections"]
    assert section["label"] == "tuning: not for claims"
    assert section["strategies"][0]["solved"] == 2
    assert any("too few tasks" in w for w in section["warnings"])
    human = invoke("eval", "report")
    assert "too few tasks" in human.stdout and "Markdown:" in human.stdout


def test_report_never_writes_into_the_arpeggio_repo(suite: Path, model: Model) -> None:
    invoke("eval", "run", "--split", "tuning", "--strategy", "middle", "--max-usd", "0.4")
    inside = Path(__file__).resolve().parents[2] / "baseline.md"
    result = invoke("eval", "report", "--markdown", str(inside), "--json")
    assert result.exit_code == 1
    assert "never go into the Arpeggio repo" in data(result)["errors"][0]["message"]
    assert not inside.exists()


def test_two_profiles_into_one_database(
    suite: Path, model: Model, tmp_path: Path, home: Path
) -> None:
    invoke("eval", "run", "--split", "tuning", "--strategy", "middle", "--max-usd", "0.4")
    free = tmp_path / "free.toml"
    free.write_bytes(template("free").encode("utf-8"))
    before = (home / "config.toml").read_bytes()
    result = invoke(
        "eval", "run", "--split", "tuning", "--strategy", "middle", "--config", str(free), "--json"
    )
    assert result.exit_code == 0, result.stdout
    assert data(result)["profile"] == "free"
    assert (home / "config.toml").read_bytes() == before
    assert free.read_bytes() == template("free").encode("utf-8")  # never written
    rep = data(invoke("eval", "report", "--json"))
    assert {s["profile"] for s in rep["sections"]} == {"free", "micro"}
    free_section = next(s for s in rep["sections"] if s["profile"] == "free")
    # the free template still has placeholder model ids: every task is skipped, not sent
    skipped = free_section["strategies"][0]["skipped"]
    assert {s["reason"].split(":")[0] for s in skipped} == {"no_allowed_route"}


def test_missing_config_file_is_a_config_error(suite: Path, tmp_path: Path) -> None:
    result = invoke("eval", "run", "--dry-run", "--config", str(tmp_path / "nope.toml"), "--json")
    assert result.exit_code == 2
    assert "config file not found" in data(result)["errors"][0]["message"]


def test_unknown_id_and_empty_runs(suite: Path) -> None:
    result = invoke("eval", "run", "--dry-run", "--id", "nope-task", "--json")
    assert result.exit_code == 1
    assert data(invoke("eval", "runs", "--json"))["runs"] == []

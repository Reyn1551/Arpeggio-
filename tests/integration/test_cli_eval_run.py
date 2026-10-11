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
from arpeggio_ai.paths import EVALS_ENV_VAR, db_path
from arpeggio_ai.store.db import open_db
from arpeggio_ai.store.repositories import list_eval_runs

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


# Edit formats (M0.8). The M0.8 PowerShell script reads `$r.runs.middle` from the JSON of
# one `eval run --strategy middle --json` and keeps the id only for a completed run.

WHOLE_ADD = (
    "FILE: src/calc/ops.py\n<<<<<<< CONTENT\ndef add(a, b):\n    return a + b\n>>>>>>> END\n"
)
WHOLE_MUL = (
    "FILE: src/calc/ops.py\n<<<<<<< CONTENT\ndef add(a, b):\n    return a + b\n\n\n"
    "def mul(a, b):\n    return a * b\n>>>>>>> END\n"
)


def test_dry_run_shows_the_edit_format(suite: Path) -> None:
    args = ["eval", "run", "--split", "tuning", "--dry-run"]
    assert data(invoke(*args, "--json"))["plan"]["edit_format"] == "udiff"
    out = data(invoke(*args, "--edit-format", "whole-file", "--json"))
    assert out["plan"]["edit_format"] == "whole-file"
    assert "edit format whole-file," in invoke(*args, "--edit-format", "whole-file").stdout
    assert invoke(*args, "--edit-format", "fuzzy").exit_code == 2


def test_dry_run_shows_the_context_selection(suite: Path) -> None:
    """M0.9: the flag defaults to manual, and auto is echoed in the plan and the header."""
    args = ["eval", "run", "--split", "tuning", "--dry-run"]
    assert data(invoke(*args, "--json"))["plan"]["context_selection"] == "manual"
    out = data(invoke(*args, "--context-selection", "auto", "--json"))
    assert out["plan"]["context_selection"] == "auto"
    assert "context selection auto:" in invoke(*args, "--context-selection", "auto").stdout
    assert invoke(*args, "--context-selection", "fuzzy").exit_code == 2


def test_run_json_exposes_the_run_id_per_strategy(
    suite: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        reply = WHOLE_MUL if "product" in user else WHOLE_ADD
        return ok(reply, usage=deepseek_usage(800, 0, 100), model=body["model"])

    monkeypatch.setattr(evals_cli, "TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    args = ["eval", "run", "--split", "tuning", "--strategy", "middle", "--max-usd", "0.4"]
    result = invoke(*args, "--edit-format", "whole-file", "--json")
    assert result.exit_code == 0, result.stdout
    out = json.loads(result.stdout)  # one JSON document on stdout, nothing else
    assert out["ok"] is True and out["status"] == "completed"
    assert set(out["runs"]) == {"middle"}
    run_id = out["runs"]["middle"]
    assert isinstance(run_id, str) and len(run_id) == 26
    assert "<<<<<<< CONTENT" in requests[0]["messages"][0]["content"]

    listed = data(invoke("eval", "runs", "--json"))["runs"]
    assert [(r["id"], r["status"], r["solved"]) for r in listed] == [(run_id, "completed", 2)]
    conn = open_db(db_path(home))
    try:
        (run,) = list_eval_runs(conn)
        assert run.summary is not None and run.summary["edit_format"] == "whole-file"
        reasons = {
            json.loads(row[0])["edit_format"]
            for row in conn.execute("SELECT route_reason FROM attempts")
        }
        assert reasons == {"whole-file"}
    finally:
        conn.close()


def test_refused_run_has_no_run_id(suite: Path, model: Model) -> None:
    args = ["eval", "run", "--split", "tuning", "--strategy", "middle"]
    result = invoke(*args, "--max-usd", "0.000001", "--edit-format", "whole-file", "--json")
    assert result.exit_code == 1
    out = json.loads(result.stdout)
    assert out["ok"] is False and "runs" not in out


def test_matrix_end_to_end(
    suite: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system, user = (m["content"] for m in body["messages"])
        if "<<<<<<< CONTENT" in system:
            reply = WHOLE_MUL if "product" in user else "No change needed."
        else:
            reply = mul_patch("a * b") if "product" in user else FIX_ADD
        return ok(reply, usage=deepseek_usage(800, 0, 100), model=body["model"])

    monkeypatch.setattr(evals_cli, "TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    args = ["eval", "run", "--split", "tuning", "--strategy", "middle", "--max-usd", "0.4"]
    ids = []
    for edit_format in ("udiff", "whole-file", "udiff", "whole-file", "udiff"):
        out = data(invoke(*args, "--edit-format", edit_format, "--json"))
        assert out["status"] == "completed"
        ids.append(out["runs"]["middle"])
    # The third whole-file run failed: its id is not in the list, and the matrix shows it.
    target = tmp_path / "matrix.md"
    result = invoke("eval", "matrix", *ids, "--markdown", str(target), "--json")
    assert result.exit_code == 0, result.stdout
    matrix = data(result)
    udiff, whole = matrix["groups"]
    assert [r["solved"] for r in udiff["runs"]] == [2, 2, 2]
    assert [(r["status"], r["solved"]) for r in whole["runs"]] == [
        ("completed", 1),
        ("completed", 1),
        ("missing", None),
    ]
    (decision,) = matrix["decisions"]
    assert decision["verdict"] == "incomplete"
    text = target.read_text("utf-8")
    assert "| whole-file #3 | `-` | missing | E |" in text
    assert "| sample-fix-add | 1 | 1 | 1 | 0 | 0 | E |" in text
    assert "patch_missing" in text
    human = invoke("eval", "matrix", *ids, "--markdown", str(target))
    assert "whole-file: solved per run 1, 1, E, mean 1.00" in human.stdout

    conn = open_db(db_path(home))
    try:
        conn.execute("UPDATE eval_runs SET git_sha = ? WHERE id = ?", ("f" * 40, ids[1]))
        conn.commit()
    finally:
        conn.close()
    refused = invoke("eval", "matrix", *ids, "--markdown", str(target), "--json")
    assert refused.exit_code == 1
    assert "runs differ in git_sha" in data(refused)["errors"][0]["message"]


# M0.8 script support: ids from the database, provider errors, retried rounds


class Switchable:
    """Answers like ``Model``, or with HTTP 429 and a Retry-After over max_quota_wait_s."""

    def __init__(self) -> None:
        self.limited = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.limited:
            return httpx.Response(429, json={"error": {}}, headers={"Retry-After": "99999"})
        body = json.loads(request.content)
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        reply = mul_patch("a * b") if "product" in user else FIX_ADD
        return ok(reply, usage=deepseek_usage(800, 0, 100), model=body["model"])


@pytest.fixture
def switchable(monkeypatch: pytest.MonkeyPatch) -> Switchable:
    fake = Switchable()
    monkeypatch.setattr(evals_cli, "TRANSPORT", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    return fake


RUN = ["eval", "run", "--split", "tuning", "--strategy", "middle", "--max-usd", "0.4"]


def test_run_json_names_sha_config_and_round(suite: Path) -> None:
    dry = data(invoke(*RUN, "--round", "2", "--dry-run", "--json"))
    assert len(dry["git_sha"]) == 40 or dry["git_sha"].startswith("unknown-")
    assert len(dry["config_hash"]) == 16 and dry["plan"]["round"] == 2
    assert invoke(*RUN, "--round", "0", "--dry-run").exit_code == 2


def test_runs_are_reconciled_from_the_database(
    suite: Path, switchable: Switchable, home: Path, tmp_path: Path
) -> None:
    before = data(invoke(*RUN, "--json"))  # an earlier run: excluded by --since
    conn = open_db(db_path(home))
    try:
        conn.execute(
            "UPDATE eval_runs SET started_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (before["runs"]["middle"],),
        )
        conn.commit()
    finally:
        conn.close()
    other = tmp_path / "other.toml"
    changed = template("micro-deepseek").replace(
        "max_quota_wait_s    = 120", "max_quota_wait_s = 121"
    )
    other.write_bytes((changed + OPT_IN).encode("utf-8"))  # a value, not a comment: new hash
    since = "2026-06-01T00:00:00.000Z"
    first = data(invoke(*RUN, "--round", "1", "--json"))
    second = data(
        invoke(
            *RUN,
            "--round",
            "2",
            "--edit-format",
            "whole-file",
            "--context-selection",
            "auto",
            "--json",
        )
    )
    data(invoke(*RUN, "--config", str(tmp_path / "other.toml"), "--json"))
    args = ["--strategy", "middle", "--git-sha", first["git_sha"]]
    args += ["--config-hash", first["config_hash"], "--since", since, "--json"]
    rows = data(invoke("eval", "runs", *args))["runs"]
    assert [r["id"] for r in rows] == [first["runs"]["middle"], second["runs"]["middle"]]
    assert [(r["edit_format"], r["context_selection"], r["round"]) for r in rows] == [
        ("udiff", "manual", 1),
        ("whole-file", "auto", 2),
    ]
    assert {r["config_hash"] for r in rows} == {first["config_hash"]}
    everything = data(invoke("eval", "runs", "--json"))["runs"]
    assert len(everything) == 4
    bad = invoke("eval", "runs", "--since", "2026-06-01T00:00:00", "--json")
    assert bad.exit_code == 1 and "needs a timezone" in data(bad)["errors"][0]["message"]


def test_provider_errors_and_a_retried_round_in_the_matrix(
    suite: Path, switchable: Switchable, tmp_path: Path
) -> None:
    ids = [data(invoke(*RUN, "--round", str(n), "--json"))["runs"]["middle"] for n in (1, 2)]
    switchable.limited = True
    limited = data(invoke(*RUN, "--round", "3", "--json"))
    assert limited["status"] == "completed"  # the run completed; its tasks hit 429
    ids.append(limited["runs"]["middle"])
    target = tmp_path / "m.md"
    matrix = data(invoke("eval", "matrix", *ids, "--markdown", str(target), "--json"))
    (udiff,) = matrix["groups"]
    hit = udiff["runs"][2]
    assert hit["provider_errors"] == 2 and set(hit["cells"].values()) == {"P"}
    assert sum(hit["failures"].values()) == 0 and hit["solved"] == 0
    assert [r["solved"] for r in udiff["runs"]] == [2, 2, 0]

    switchable.limited = False
    ids.append(data(invoke(*RUN, "--round", "3", "--json"))["runs"]["middle"])
    matrix = data(invoke("eval", "matrix", *ids, "--markdown", str(target), "--json"))
    (udiff,) = matrix["groups"]
    assert [(r["round"], r["solved"], r["provider_errors"]) for r in udiff["runs"]] == [
        (1, 2, 0),
        (2, 2, 0),
        (3, 2, 0),
    ]
    assert [s["run_id"] for s in udiff["superseded"]] == [limited["runs"]["middle"]]
    assert "udiff r4" not in target.read_text("utf-8")

"""Eval runs end to end on the sample suite with a fake model (EVL-03, VER-03, CST-05)."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import deepseek_usage, ok, status
from runkit import RunHarness, write_report

from arpeggio_ai.evals.budget import BudgetError, check_estimate
from arpeggio_ai.evals.runner import counterfactual_usd
from arpeggio_ai.store.repositories import (
    get_task,
    list_eval_results,
    list_eval_runs,
    list_steps,
    list_task_attempts,
)

TASKS = ["sample-fix-add", "sample-feature-mul"]


@pytest.fixture
def h(tmp_path: Path, home: Path) -> Iterator[RunHarness]:
    harness = RunHarness(tmp_path, home)
    harness.valid(*TASKS)
    yield harness
    harness.close()


def results(h: RunHarness) -> dict[str, dict[str, tuple[str, int]]]:
    """strategy -> eval task -> (status, attempts)"""
    out: dict[str, dict[str, tuple[str, int]]] = {}
    for run in list_eval_runs(h.conn):
        out[run.strategy] = {
            r.eval_task: (r.status, r.attempts) for r in list_eval_results(h.conn, run.id)
        }
    return out


def test_all_four_strategies_solve_and_escalate_as_specified(h: RunHarness) -> None:
    outcome = h.run(h.plan())
    assert outcome.status == "completed" and outcome.reason is None
    assert results(h) == {
        "junior": {"sample-fix-add": ("solved", 1), "sample-feature-mul": ("solved", 1)},
        "middle": {"sample-fix-add": ("solved", 1), "sample-feature-mul": ("failed", 1)},
        "senior": {"sample-fix-add": ("solved", 2), "sample-feature-mul": ("solved", 3)},
        "arpeggio": {"sample-fix-add": ("solved", 1), "sample-feature-mul": ("solved", 3)},
    }
    # tasks interleave strategies; within a task the routes follow the plan
    # tasks run in ID order (feature-mul first), strategies interleaved within each task
    assert h.model.models() == [
        "m-tier3.pro",  # junior, mul
        "m-tier2.flash",  # middle: checks fail, no escalation
        "m-tier1.flash",  # senior: patch_missing, escalates
        "m-tier2.flash",
        "m-tier3.pro",
        "m-tier2.flash",  # arpeggio: medium, high, strongest
        "m-tier2.flash",
        "m-tier3.pro",
        "m-tier3.pro",  # junior, fix-add
        "m-tier2.flash",  # middle
        "m-tier1.flash",  # senior
        "m-tier2.flash",
        "m-tier2.flash",  # arpeggio
    ]
    efforts = [b.get("thinking", {}).get("type") for b in h.model.requests]
    assert len(efforts) == 13
    assert h.worktrees() == []  # removed after each attempt
    runs = list_eval_runs(h.conn)
    assert {r.status for r in runs} == {"completed"}
    assert all(r.planned == 2 and r.repeats == 1 and r.profile == "micro" for r in runs)


def test_recording_links_tasks_attempts_and_costs(h: RunHarness) -> None:
    h.run(h.plan(ids=["sample-feature-mul"], strategies=("senior",)))
    run = list_eval_runs(h.conn)[0]
    (result,) = list_eval_results(h.conn, run.id)
    assert (result.status, result.attempts, result.escalations) == ("solved", 3, 2)
    assert result.tags == ["python", "sample"]
    assert result.task_id is not None
    task = get_task(h.conn, result.task_id)
    assert task is not None
    assert (task.source, task.category, task.risk, task.profile) == (
        "eval",
        "feature",
        "low",
        "micro",
    )
    attempts = list_task_attempts(h.conn, task.id)
    assert [a.model for a in attempts] == ["tier1.flash", "tier2.flash", "tier3.pro"]
    assert [a.failure_reason for a in attempts] == ["patch_missing", None, None]
    assert [a.route_reason["step"] for a in attempts] == [0, 1, 2]
    assert all(a.route_reason["strategy"] == "senior" for a in attempts)
    for attempt in attempts:
        steps = list_steps(h.conn, attempt.id)
        assert attempt.cost_usd == pytest.approx(sum(s.cost_usd or 0 for s in steps))
        assert attempt.input_tokens == sum(s.input_tokens or 0 for s in steps)
    assert result.cost_usd == pytest.approx(sum(a.cost_usd for a in attempts))
    assert result.cost_usd > 0
    steps = [s for a in attempts for s in list_steps(h.conn, a.id)]
    expected = counterfactual_usd(h.config, steps)
    assert result.counterfactual_usd == expected == task.counterfactual_usd
    assert expected > 0
    assert result.estimate_usd is not None and result.estimate_usd >= result.cost_usd


def test_arpeggio_rule_id_is_recorded(h: RunHarness) -> None:
    h.run(h.plan(ids=["sample-fix-add"], strategies=("arpeggio",)))
    run = list_eval_runs(h.conn)[0]
    (result,) = list_eval_results(h.conn, run.id)
    assert result.task_id is not None
    (attempt,) = list_task_attempts(h.conn, result.task_id)
    assert attempt.route_reason["rule"] == "low-risk-code"
    assert (attempt.model, attempt.effort) == ("tier2.flash", "medium")


def test_provider_error_ends_the_task_without_escalation(h: RunHarness) -> None:
    h.model.reply = lambda model, prompt: status(400)
    h.run(h.plan(ids=["sample-fix-add"], strategies=("senior",)))
    assert results(h)["senior"]["sample-fix-add"] == ("error", 1)
    assert len(h.model.requests) == 1


def test_max_three_attempts(h: RunHarness) -> None:
    h.model.reply = lambda model, prompt: ok("no diff here", usage=deepseek_usage())
    h.run(h.plan(ids=["sample-fix-add"], strategies=("senior", "arpeggio")))
    got = results(h)
    assert got["senior"]["sample-fix-add"] == ("failed", 3)
    assert got["arpeggio"]["sample-fix-add"] == ("failed", 3)
    run = next(r for r in list_eval_runs(h.conn) if r.strategy == "senior")
    (result,) = list_eval_results(h.conn, run.id)
    assert result.status_reason == "patch_missing"


def test_escalated_prompt_has_the_failure_report(h: RunHarness) -> None:
    h.run(h.plan(ids=["sample-feature-mul"], strategies=("arpeggio",)))
    prompts = [
        next(m["content"] for m in body["messages"] if m["role"] == "user")
        for body in h.model.requests
    ]
    assert "Previous attempt" not in prompts[0]
    assert "Attempt 1 failed: checks_failed." in prompts[1]
    assert "- check 1: exit code 1" in prompts[1]
    assert "Attempt 2 failed: checks_failed." in prompts[2]


MARKER = "HIDDEN-ASSERTION-MARKER-4242"


def test_hidden_test_output_never_reaches_a_prompt_or_artifact(h: RunHarness) -> None:
    hidden = (
        "import sys\nimport unittest\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))\n\n"
        "from calc.ops import mul\n\n\n"
        "class SecretTest(unittest.TestCase):\n"
        "    def test_mul(self):\n"
        f"        print('{MARKER}')\n"
        f"        self.assertEqual(mul(6, 7), 42, '{MARKER}')\n\n\n"
        "unittest.main()\n"
    )
    source = h.suite.write_fixture(
        "fixtures/secret-mul/hidden/tests/test_secret.py", hidden.encode()
    )
    py = sys.executable
    data = h.suite.base_task(
        "secret-mul",
        category="feature",
        repo={"path": "${SAMPLE_REPO}", "base": "${SAMPLE_C1}", "solution": "${SAMPLE_C2}"},
        request="Add mul(a, b) to calc.ops returning the product.",
        done_criteria=[
            {"kind": "command", "argv": [py, "tests/test_secret.py"]},
            {"kind": "command", "argv": [py, "-m", "unittest", "discover", "-s", "tests"]},
            {"kind": "file_exists", "path": "docs/missing.md"},
        ],
        hidden_tests=[{"path": "tests/test_secret.py", "source": source}],
        context_files=["src/calc/ops.py"],
    )
    h.suite.write_task(data)
    h.valid("secret-mul")
    h.run(h.plan(ids=["secret-mul"], strategies=("arpeggio",)))
    assert len(h.model.requests) == 3
    escalated = str(h.model.requests[1])
    assert "check 1: exit code" in escalated and "check 3: exit code" in escalated
    for body in h.model.requests:
        assert MARKER not in str(body)
        assert "SecretTest" not in str(body)  # the file's contents never go out
    assert "  output withheld" in escalated  # check 1 names the hidden test
    # the hidden test did run and print the marker: its log holds it, the prompts do not
    artifacts = list((h.home / "artifacts").rglob("*"))
    logs = [p for p in artifacts if p.is_file() and p.name.startswith("check-")]
    assert any(MARKER in p.read_text("utf-8", errors="replace") for p in logs)
    for path in artifacts:
        if path.is_file() and (path.name.startswith(("prompt", "step-"))):
            assert MARKER not in path.read_text("utf-8", errors="replace")


def test_setup_failure_is_an_error_not_an_escalation(h: RunHarness) -> None:
    py = sys.executable
    data = h.suite.base_task(
        "setup-breaks", setup=[{"argv": [py, "-c", "import sys; sys.exit(3)"]}]
    )
    h.suite.write_task(data)
    h.valid("setup-breaks")
    h.run(h.plan(ids=["setup-breaks"], strategies=("senior",)))
    run = list_eval_runs(h.conn)[0]
    (result,) = list_eval_results(h.conn, run.id)
    assert (result.status, result.attempts, result.status_reason) == ("error", 2, "setup_failed")


# Selection


def test_only_current_valid_self_checks_run(h: RunHarness, tmp_path: Path) -> None:
    write_report(h.root, {"sample-fix-add": "unsolvable"})
    plan = h.plan()
    assert [t.task.id for t in plan.tasks] == ["sample-feature-mul"]
    (skipped,) = plan.skipped
    assert (skipped.id, skipped.reason) == ("sample-fix-add", "unsolvable")


def test_stale_self_check_is_skipped(h: RunHarness) -> None:
    task_file = h.root / "tasks" / "sample-fix-add.yaml"
    task_file.write_text(task_file.read_text("utf-8") + "\n# edited\n", encoding="utf-8")
    hidden = h.root / "fixtures" / "sample-feature-mul" / "hidden" / "tests" / "test_mul.py"
    hidden.touch()
    plan = h.plan()
    assert plan.tasks == []
    reasons = {s.id: (s.reason, s.message) for s in plan.skipped}
    assert reasons["sample-fix-add"][0] == "stale_self_check"
    assert "arpeggio eval check --id sample-fix-add" in reasons["sample-fix-add"][1]
    assert reasons["sample-feature-mul"][0] == "stale_self_check"
    h.valid(*TASKS)
    assert len(h.plan().tasks) == 2


def test_unchecked_task_is_skipped(h: RunHarness) -> None:
    h.suite.write_task(h.suite.base_task("never-checked"))
    plan = h.plan(ids=["never-checked"])
    assert [(s.id, s.reason) for s in plan.skipped] == [("never-checked", "unchecked")]


def test_no_allowed_route_is_recorded_as_skip(tmp_path: Path, home: Path) -> None:
    from routekit import build_config

    h = RunHarness(tmp_path, home, build_config(repo={"privacy_class": "private"}))
    try:
        h.valid(*TASKS)
        plan = h.plan(ids=["sample-fix-add"])
        assert plan.estimate_usd == 0
        skip = plan.tasks[0].strategies["middle"].skip
        assert skip is not None and skip.startswith("no_allowed_route:")
        assert "allow_training_providers = true" in skip
        h.run(plan)
        assert h.model.requests == []
        for run in list_eval_runs(h.conn):
            (result,) = list_eval_results(h.conn, run.id)
            assert result.status == "skipped" and result.task_id is None
    finally:
        h.close()


# Budget


def test_dry_run_plan_makes_no_calls_and_no_worktrees(h: RunHarness) -> None:
    plan = h.plan(repeats=3)
    assert h.model.requests == [] and h.worktrees() == []
    data = plan.to_dict()
    assert data["planned_task_runs"] == 6
    fix = next(t for t in data["tasks"] if t["id"] == "sample-fix-add")
    assert [r["model"] for r in fix["strategies"]["senior"]["routes"]] == [
        "tier1.flash",
        "tier2.flash",
        "tier3.pro",
    ]
    assert fix["strategies"]["arpeggio"]["routes"][0]["rule"] == "low-risk-code"
    per_strategy = data["estimate_per_strategy_usd"]
    assert sum(per_strategy.values()) == pytest.approx(plan.estimate_usd)
    # senior may try three routes, junior one: the bound reflects it
    assert per_strategy["senior"] > per_strategy["junior"]
    one = h.plan(repeats=1)
    assert plan.estimate_usd == pytest.approx(3 * one.estimate_usd)


def test_estimate_refusal_before_start(h: RunHarness) -> None:
    plan = h.plan()
    with pytest.raises(BudgetError, match="worst-case estimate"):
        check_estimate(plan.estimate_usd, plan.estimate_usd / 2)
    check_estimate(plan.estimate_usd, plan.estimate_usd)


def test_cap_reached_mid_run_marks_runs_partial(h: RunHarness) -> None:
    plan = h.plan(ids=["sample-fix-add"], strategies=("senior", "junior"))
    first, second, _ = plan.tasks[0].strategies["senior"].estimates_usd
    assert second > first  # the escalated prompt reserves room for the failure report
    # room for senior's first attempt only: its escalation and junior must not start
    outcome = h.run(plan, limit_usd=first)
    assert outcome.status == "partial"
    assert outcome.reason is not None and outcome.reason.startswith("budget_cap")
    assert 0 < outcome.spent_usd <= first
    assert {r.status for r in list_eval_runs(h.conn)} == {"partial"}
    got = results(h)
    assert got["senior"]["sample-fix-add"] == ("paused", 1)
    assert got["junior"] == {}
    assert h.model.models() == ["m-tier1.flash"]
    run = next(r for r in list_eval_runs(h.conn) if r.strategy == "senior")
    (result,) = list_eval_results(h.conn, run.id)
    assert result.status_reason == "budget_cap"


def test_cap_too_small_for_the_first_attempt_runs_nothing(h: RunHarness) -> None:
    outcome = h.run(h.plan(ids=["sample-fix-add"]), limit_usd=1e-9)
    assert outcome.status == "partial" and h.model.requests == []
    for run in list_eval_runs(h.conn):
        assert list_eval_results(h.conn, run.id) == []


def test_repeats_get_their_own_rows(h: RunHarness) -> None:
    h.run(h.plan(ids=["sample-fix-add"], strategies=("middle",), repeats=2))
    run = list_eval_runs(h.conn)[0]
    rows = list_eval_results(h.conn, run.id)
    assert [(r.repeat_index, r.status) for r in rows] == [(0, "solved"), (1, "solved")]
    assert len({r.task_id for r in rows}) == 2


def test_sleep_between_attempts(h: RunHarness) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    h.run(h.plan(ids=["sample-fix-add"], strategies=("senior",)), sleep_between_s=2.5, sleep=sleep)
    assert slept == [2.5]  # two attempts, one pause between them


def test_monthly_spend_counts_eval_attempts(h: RunHarness) -> None:
    from arpeggio_ai.store.repositories import month_eval_spend_usd

    outcome = h.run(h.plan(ids=["sample-fix-add"], strategies=("junior",)))
    run = list_eval_runs(h.conn)[0]
    month = run.started_at[:7]
    assert month_eval_spend_usd(h.conn, month) == pytest.approx(outcome.spent_usd)

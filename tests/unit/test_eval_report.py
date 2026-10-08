"""Eval report on a constructed result set with known answers (EVL-05, EVL-06)."""

import sqlite3
from pathlib import Path

import pytest

from arpeggio_ai.evals.report import (
    FAILURE_KINDS,
    Interval,
    TaskRun,
    bootstrap,
    build_report,
    cost_per_solved,
    paired_bootstrap,
    render_markdown,
    success_rate,
)
from arpeggio_ai.store.repositories import (
    add_eval_result,
    add_verdict,
    append_step,
    create_attempt,
    create_eval_run,
    create_task,
    finish_eval_run,
    register_repo,
    set_attempt_status,
    set_failure_reason,
    set_model_mismatch,
)

# eval task -> (tags, category, risk)
TASKS = {
    "a": (["check:lint", "difficulty:easy"], "docs", "low"),
    "b": (["check:lint", "difficulty:hard"], "bugfix", "low"),
    "c": (["difficulty:hard"], "feature", "medium"),
    "d": ([], "feature", "high"),
}


def make_run(
    conn: sqlite3.Connection,
    repo_id: str,
    strategy: str,
    outcomes: dict[str, list[tuple[bool, float]]],
    *,
    profile: str = "micro",
    split: str = "holdout",
    status: str = "completed",
    mismatch_on: str | None = None,
) -> str:
    run = create_eval_run(
        conn,
        strategy=strategy,
        split=split,  # type: ignore[arg-type]
        git_sha="x",
        config_hash="h",
        profile=profile,  # type: ignore[arg-type]
        repeats=max(len(v) for v in outcomes.values()),
        planned=4,
        estimate_usd=1.0,
    )
    for name, repeats in outcomes.items():
        tags, category, risk = TASKS[name]
        for index, (solved, cost) in enumerate(repeats):
            task = create_task(
                conn,
                repo_id,
                name,
                "r",
                profile=profile,  # type: ignore[arg-type]
                source="eval",
                category=category,
                risk=risk,  # type: ignore[arg-type]
            )
            attempt = create_attempt(
                conn,
                task.id,
                adapter="api",
                model="tier1.x",
                effort="low",
                verification="light",
                route_reason={},
            )
            append_step(conn, attempt.id, "model_call", cost_usd=cost, cost_estimated=name == "b")
            if name == mismatch_on:
                set_model_mismatch(conn, attempt.id, True)
            add_eval_result(
                conn,
                run.id,
                name,
                repeat_index=index,
                status="solved" if solved else "failed",
                task_id=task.id,
                cost_usd=cost,
                attempts=2 if name == "c" else 1,
                duration_s=10.0,
                escalations=1 if name == "c" else 0,
                counterfactual_usd=cost * 4,
                tags=tags,
            )
    if status != "completed":
        finish_eval_run(conn, run.id, status, reason="budget_cap: stopped")  # type: ignore[arg-type]
    else:
        finish_eval_run(conn, run.id, "completed")
    return run.id


@pytest.fixture
def repo_id(db: sqlite3.Connection, tmp_path: Path) -> str:
    return register_repo(db, tmp_path).id


MIDDLE = {"a": [(True, 0.1)], "b": [(True, 0.1)], "c": [(False, 0.1)], "d": [(False, 0.1)]}
ARPEGGIO = {"a": [(True, 0.05)], "b": [(True, 0.05)], "c": [(True, 0.05)], "d": [(False, 0.05)]}


def test_metrics_with_known_answers(db: sqlite3.Connection, repo_id: str) -> None:
    make_run(db, repo_id, "middle", MIDDLE)
    make_run(db, repo_id, "arpeggio", ARPEGGIO, mismatch_on="a")
    report = build_report(db, resamples=200)
    (section,) = report.sections
    assert (section.profile, section.split, section.tasks) == ("micro", "holdout", 4)
    assert section.label == "headline (holdout)"
    middle, arpeggio = section.strategies
    assert [middle.strategy, arpeggio.strategy] == ["middle", "arpeggio"]
    assert (middle.task_runs, middle.solved, middle.success_rate) == (4, 2, 0.5)
    assert middle.total_cost_usd == pytest.approx(0.4)
    assert middle.cost_per_solved_usd == pytest.approx(0.2)
    assert arpeggio.success_rate == 0.75
    assert arpeggio.cost_per_solved_usd == pytest.approx(0.2 / 3)
    assert arpeggio.mean_attempts == pytest.approx(1.25)
    assert arpeggio.escalations == 1
    assert arpeggio.wall_clock_s == 40.0 and arpeggio.quota_wait_s == 0.0
    assert arpeggio.estimated_cost_share == pytest.approx(0.25)  # task b's cost was estimated
    assert arpeggio.model_mismatches == 1
    assert arpeggio.counterfactual_usd == pytest.approx(0.8)
    assert arpeggio.savings_vs_counterfactual_usd == pytest.approx(0.6)
    (paired,) = section.paired
    assert paired.strategy == "arpeggio" and paired.tasks == 4
    assert paired.success_diff == pytest.approx(0.25)
    assert paired.cost_per_solved_diff_usd == pytest.approx(0.2 / 3 - 0.2)
    assert any("too few tasks" in w for w in section.warnings)


def test_bootstrap_is_reproducible_with_a_seed(db: sqlite3.Connection, repo_id: str) -> None:
    make_run(db, repo_id, "middle", MIDDLE)
    make_run(db, repo_id, "arpeggio", ARPEGGIO)
    one = build_report(db, seed=7, resamples=500).to_dict()
    two = build_report(db, seed=7, resamples=500).to_dict()
    other = build_report(db, seed=8, resamples=500).to_dict()
    assert one == two
    assert one["seed"] == 7
    assert one != other


def run(name: str, solved: bool, cost: float, repeat: int = 0) -> TaskRun:
    return TaskRun(
        name,
        repeat,
        "solved" if solved else "failed",
        solved,
        cost,
        1,
        0,
        1.0,
        0.0,
        0.0,
        0.0,
        0,
        0.0,
        (),
        "bugfix",
        "low",
    )


def test_bootstrap_keeps_repeats_together_and_bounds_the_point() -> None:
    by_task = {
        "a": [run("a", True, 1.0), run("a", True, 1.0, 1)],
        "b": [run("b", False, 1.0), run("b", True, 1.0, 1)],
        "c": [run("c", False, 1.0), run("c", False, 1.0, 1)],
    }
    point = success_rate(list(by_task.values()))
    ci = bootstrap(by_task, success_rate, seed=1, resamples=2000)
    assert ci.low is not None and ci.high is not None
    assert ci.low <= point <= ci.high  # type: ignore[operator]
    # every resample has 6 runs (three tasks of two repeats), so rates are multiples of 1/6
    assert round(ci.low * 6, 9) == round(ci.low * 6)
    assert ci.dropped_share == 0 and not ci.unreliable


def test_cost_per_solved_ci_drops_resamples_without_a_solve() -> None:
    by_task = {name: [run(name, name == "a", 1.0)] for name in "abcd"}
    ci = bootstrap(by_task, cost_per_solved, seed=3, resamples=4000)
    # P(no "a" in 4 draws) = (3/4)^4 = 0.316
    assert ci.dropped_share == pytest.approx(0.316, abs=0.03)
    assert ci.unreliable
    none = bootstrap({"x": [run("x", False, 1.0)]}, cost_per_solved, seed=3, resamples=50)
    assert none == Interval(None, None, 1.0, True)


def test_paired_bootstrap_uses_common_tasks_only() -> None:
    candidate = {"a": [run("a", True, 1.0)], "b": [run("b", True, 1.0)], "z": [run("z", False, 9)]}
    reference = {"a": [run("a", True, 2.0)], "b": [run("b", False, 2.0)]}
    point, ci = paired_bootstrap(candidate, reference, success_rate, seed=1, resamples=500)
    assert point == pytest.approx(0.5)
    assert ci.low is not None and ci.low >= 0
    point, ci = paired_bootstrap(candidate, reference, cost_per_solved, seed=1, resamples=500)
    assert point == pytest.approx(1.0 - 4.0)


def test_unreliable_ci_is_labeled_in_markdown(db: sqlite3.Connection, repo_id: str) -> None:
    make_run(db, repo_id, "middle", MIDDLE)
    make_run(db, repo_id, "junior", {name: [(name == "a", 0.3)] for name in "abcd"})
    report = build_report(db, resamples=1000)
    junior = report.sections[0].strategies[0]
    assert junior.strategy == "junior" and junior.cost_per_solved_ci.unreliable
    markdown = render_markdown(report)
    assert "unreliable:" in markdown and "had no solved task" in markdown
    assert report.to_dict()["sections"][0]["strategies"][0]["cost_per_solved_ci"]["unreliable"]


def test_partial_runs_and_profiles_and_splits(db: sqlite3.Connection, repo_id: str) -> None:
    make_run(db, repo_id, "middle", MIDDLE, status="partial")
    make_run(db, repo_id, "middle", MIDDLE, profile="free", split="tuning")
    report = build_report(db, resamples=100)
    sections = {(s.profile, s.split): s for s in report.sections}
    assert set(sections) == {("micro", "holdout"), ("free", "tuning")}
    assert sections[("free", "tuning")].label == "tuning: not for claims"
    partial = sections[("micro", "holdout")]
    assert partial.strategies[0].partial_reasons == ["budget_cap: stopped"]
    assert any("partial run" in w for w in partial.warnings)
    markdown = render_markdown(report)
    assert "middle (PARTIAL)" in markdown and "is PARTIAL: budget_cap: stopped" in markdown
    assert "## Profile `free`, split `tuning`" in markdown


def test_latest_run_per_strategy_unless_ids_given(db: sqlite3.Connection, repo_id: str) -> None:
    old = make_run(db, repo_id, "middle", {"a": [(False, 1.0)]})
    new = make_run(db, repo_id, "middle", MIDDLE)
    assert build_report(db, resamples=10).run_ids == [new]
    assert build_report(db, [old], resamples=10).sections[0].strategies[0].solved == 0
    with pytest.raises(ValueError, match="unknown eval runs"):
        build_report(db, ["nope"])


def test_breakdowns_by_tags_with_fallbacks(db: sqlite3.Connection, repo_id: str) -> None:
    make_run(db, repo_id, "middle", MIDDLE)
    make_run(db, repo_id, "arpeggio", ARPEGGIO)
    section = build_report(db, resamples=10).sections[0]
    by = {b.dimension: b for b in section.breakdowns}
    check = by["check"]
    assert check.source == "check:"
    assert [(r.group, r.tasks) for r in check.rows] == [("(none)", 2), ("check:lint", 2)]
    lint = check.rows[1]
    assert lint.indicative_only
    assert lint.values["middle"] == (1.0, pytest.approx(0.1))
    assert by["difficulty"].source == "difficulty:"
    hard = next(r for r in by["difficulty"].rows if r.group == "difficulty:hard")
    assert hard.values["arpeggio"] == (1.0, pytest.approx(0.05))
    assert hard.values["middle"] == (0.5, pytest.approx(0.2))
    stack = by["stack"]
    assert stack.source == "expected_risk"  # no stack: tags, so expected risk
    assert [(r.group, r.tasks) for r in stack.rows] == [("high", 1), ("low", 2), ("medium", 1)]
    markdown = render_markdown(build_report(db, resamples=10))
    assert "### By check (`check:`)" in markdown
    assert "### By stack (`expected_risk`)" in markdown
    assert "| check:lint (indicative only) | 2 |" in markdown


def test_breakdown_falls_back_to_category_without_difficulty_tags(
    db: sqlite3.Connection, repo_id: str
) -> None:
    TASKS["e"] = (["stack:laravel"], "config", "low")
    try:
        make_run(db, repo_id, "middle", {"e": [(True, 0.1)], "d": [(False, 0.1)]})
        by = {b.dimension: b for b in build_report(db, resamples=10).sections[0].breakdowns}
        assert "check" not in by  # no check: tags at all
        assert by["difficulty"].source == "category"
        assert {r.group for r in by["difficulty"].rows} == {"config", "feature"}
        assert by["stack"].source == "stack:"
        assert {r.group for r in by["stack"].rows} == {"(none)", "stack:laravel"}
    finally:
        del TASKS["e"]


def test_skipped_tasks_are_listed(db: sqlite3.Connection, repo_id: str) -> None:
    run_id = make_run(db, repo_id, "middle", MIDDLE)
    add_eval_result(
        db, run_id, "zz", repeat_index=0, status="skipped", status_reason="stale_self_check: x"
    )
    report = build_report(db, resamples=10)
    assert report.sections[0].strategies[0].skipped == [
        {"task": "zz", "reason": "stale_self_check: x"}
    ]
    assert report.sections[0].strategies[0].task_runs == 4
    assert "- `zz`: stale_self_check: x" in render_markdown(report)


def test_section_without_middle_warns(db: sqlite3.Connection, repo_id: str) -> None:
    make_run(db, repo_id, "junior", MIDDLE)
    section = build_report(db, resamples=10).sections[0]
    assert section.paired == []
    assert any("no middle run" in w for w in section.warnings)


def test_attempt_outcomes_and_reasoning_share(db: sqlite3.Connection, repo_id: str) -> None:
    run = create_eval_run(
        db,
        strategy="senior",
        split="holdout",
        git_sha="x",
        config_hash="h",
        profile="micro",
        repeats=1,
        planned=1,
        estimate_usd=1.0,
    )
    task = create_task(db, repo_id, "a", "r", profile="micro", source="eval")
    # attempt -> (failure_reason, verdict passed, (output, reasoning) per model call)
    plan: list[tuple[str | None, bool | None, list[tuple[int, int | None]]]] = [
        ("output_truncated", None, [(4096, 4096)]),
        ("output_truncated", None, [(4096, 4000)]),
        ("patch_missing", None, [(100, None)]),  # reports no reasoning: left out of the share
        ("patch_does_not_apply", None, [(300, 100)]),
        (None, False, [(500, 300)]),  # checks_failed
        (None, True, [(400, 0)]),  # solved: no failure kind
        ("patch_unsafe", None, [(50, None)]),  # not a listed kind
    ]
    for reason, passed, calls in plan:
        attempt = create_attempt(
            db,
            task.id,
            adapter="api",
            model="tier1.x",
            effort="low",
            verification="light",
            route_reason={},
        )
        for output, reasoning in calls:
            append_step(
                db,
                attempt.id,
                "model_call",
                output_tokens=output,
                reasoning_tokens=reasoning,
                finish_reason="length" if reason == "output_truncated" else "stop",
                cost_usd=0.01,
            )
        if reason is not None:
            set_failure_reason(db, attempt.id, reason)
            set_attempt_status(db, attempt.id, "error")
        else:
            add_verdict(db, attempt.id, kind="check", passed=bool(passed))
            set_attempt_status(db, attempt.id, "completed")
    add_eval_result(
        db,
        run.id,
        "a",
        status="solved",
        task_id=task.id,
        cost_usd=0.07,
        attempts=7,
        duration_s=1.0,
        escalations=6,
        repeat_index=0,
    )
    finish_eval_run(db, run.id, "completed")
    (section,) = build_report(db, resamples=50).sections
    (senior,) = section.strategies
    assert senior.failures == {
        "output_truncated": 2,
        "patch_missing": 1,
        "patch_does_not_apply": 1,
        "checks_failed": 1,
    }
    assert senior.reasoning_tokens == 4096 + 4000 + 100 + 300
    assert senior.reasoning_share == pytest.approx(8496 / (4096 + 4096 + 300 + 500 + 400))
    markdown = render_markdown(build_report(db, resamples=50))
    assert "### Attempt outcomes and reasoning" in markdown
    assert "| senior | 2 | 1 | 1 | 1 | 8496 | 90% |" in markdown


def test_reasoning_share_is_na_without_reports(db: sqlite3.Connection, repo_id: str) -> None:
    make_run(db, repo_id, "middle", MIDDLE)
    (middle,) = build_report(db, resamples=50).sections[0].strategies
    assert middle.reasoning_share is None and middle.reasoning_tokens == 0
    assert middle.failures == dict.fromkeys(FAILURE_KINDS, 0)

"""Repositories for eval runs and results, and the monthly eval spend (M0.6)."""

import sqlite3
from typing import Any

import pytest

from arpeggio_ai.core.errors import StoreError
from arpeggio_ai.store.repositories import (
    EvalRun,
    Task,
    add_eval_result,
    add_verdict,
    append_step,
    create_attempt,
    create_eval_run,
    create_task,
    finish_eval_run,
    get_task,
    list_eval_results,
    list_eval_runs,
    month_eval_spend_usd,
    register_repo,
    set_counterfactual,
    task_cost_usd,
)

ROUTE: dict[str, Any] = {
    "adapter": "api",
    "model": "tier1.flash",
    "effort": "low",
    "verification": "light",
    "route_reason": {"rule": "x"},
}


@pytest.fixture
def task(db: sqlite3.Connection, tmp_path: Any) -> Task:
    repo = register_repo(db, tmp_path)
    return create_task(
        db, repo.id, "t", "r", profile="micro", source="eval", category="bugfix", risk="low"
    )


@pytest.fixture
def run(db: sqlite3.Connection) -> EvalRun:
    return create_eval_run(
        db,
        strategy="middle",
        split="holdout",
        git_sha="abc",
        config_hash="h",
        profile="micro",
        repeats=2,
        planned=4,
        estimate_usd=0.5,
    )


def spend(db: sqlite3.Connection, task_id: str, cost: float, verdict_cost: float = 0.0) -> None:
    attempt = create_attempt(db, task_id, **ROUTE)
    append_step(db, attempt.id, "model_call", cost_usd=cost)
    add_verdict(db, attempt.id, kind="model_review", passed=True, cost_usd=verdict_cost)


def test_task_stores_risk(task: Task) -> None:
    assert (task.risk, task.source, task.category) == ("low", "eval", "bugfix")


def test_task_rejects_unknown_risk(db: sqlite3.Connection, task: Task) -> None:
    with pytest.raises(StoreError, match="risk"):
        create_task(db, task.repo_id, "t", "r", profile="micro", risk="huge")  # type: ignore[arg-type]


def test_eval_run_lifecycle(db: sqlite3.Connection, run: EvalRun) -> None:
    assert (run.status, run.repeats, run.planned, run.estimate_usd) == ("running", 2, 4, 0.5)
    done = finish_eval_run(db, run.id, "partial", reason="budget_cap", summary={"n": 1})
    assert (done.status, done.status_reason, done.summary) == ("partial", "budget_cap", {"n": 1})
    assert done.finished_at is not None
    assert [r.id for r in list_eval_runs(db)] == [run.id]


def test_eval_run_rejects_unknown_status(db: sqlite3.Connection, run: EvalRun) -> None:
    with pytest.raises(StoreError, match="status"):
        finish_eval_run(db, run.id, "done")  # type: ignore[arg-type]


def test_eval_results_per_repeat(db: sqlite3.Connection, run: EvalRun, task: Task) -> None:
    add_eval_result(
        db, run.id, "t1", repeat_index=0, status="solved", task_id=task.id, tags=["check:x"]
    )
    add_eval_result(db, run.id, "t1", repeat_index=1, status="failed", task_id=task.id)
    add_eval_result(
        db, run.id, "t2", repeat_index=0, status="skipped", status_reason="no_allowed_route"
    )
    results = list_eval_results(db, run.id)
    assert [(r.eval_task, r.repeat_index, r.solved, r.status) for r in results] == [
        ("t1", 0, True, "solved"),
        ("t1", 1, False, "failed"),
        ("t2", 0, False, "skipped"),
    ]
    assert results[0].tags == ["check:x"]
    assert results[2].task_id is None
    with pytest.raises(StoreError, match="cannot add"):
        add_eval_result(db, run.id, "t1", repeat_index=0, status="failed", task_id=task.id)
    with pytest.raises(StoreError, match="only a skipped"):
        add_eval_result(db, run.id, "t3", repeat_index=0, status="failed")


def test_counterfactual_and_task_cost(db: sqlite3.Connection, task: Task) -> None:
    spend(db, task.id, 0.25, verdict_cost=0.05)
    spend(db, task.id, 0.5)
    assert task_cost_usd(db, task.id) == pytest.approx(0.8)
    set_counterfactual(db, task.id, 1.5)
    stored = get_task(db, task.id)
    assert stored is not None and stored.counterfactual_usd == 1.5
    with pytest.raises(StoreError):
        set_counterfactual(db, task.id, -1)


def test_month_eval_spend_counts_only_linked_tasks(
    db: sqlite3.Connection, run: EvalRun, task: Task
) -> None:
    other = create_task(db, task.repo_id, "user task", "r", profile="micro")
    spend(db, task.id, 0.25, verdict_cost=0.05)
    spend(db, other.id, 9.0)
    month = task.created_at[:7]
    assert month_eval_spend_usd(db, month) == 0.0  # not linked yet
    add_eval_result(db, run.id, "t1", repeat_index=0, status="failed", task_id=task.id)
    assert month_eval_spend_usd(db, month) == pytest.approx(0.30)
    assert month_eval_spend_usd(db, "1999-01") == 0.0

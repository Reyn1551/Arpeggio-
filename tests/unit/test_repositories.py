import sqlite3
from typing import Any

import pytest

from arpeggio_ai.core.errors import StoreError
from arpeggio_ai.store.repositories import (
    Attempt,
    Repo,
    Task,
    append_step,
    create_attempt,
    create_task,
    ensure_repo,
    get_attempt,
    get_task,
    list_steps,
    set_attempt_status,
    set_deferred_until,
    set_model_mismatch,
    set_task_status,
)

ROUTE: dict[str, Any] = {
    "adapter": "api",
    "model": "tier1.cheap",
    "effort": "low",
    "verification": "light",
    "route_reason": {"rule": "docs-and-format"},
}


@pytest.fixture
def repo(db: sqlite3.Connection) -> Repo:
    return ensure_repo(db, "/code/app", "app")


@pytest.fixture
def task(db: sqlite3.Connection, repo: Repo) -> Task:
    return create_task(
        db, repo.id, "Add validation", "add email validation to signup", profile="micro"
    )


@pytest.fixture
def attempt(db: sqlite3.Connection, task: Task) -> Attempt:
    return create_attempt(db, task.id, **ROUTE)


# Repos


def test_ensure_repo_inserts_with_defaults(repo: Repo) -> None:
    assert repo.path == "/code/app"
    assert repo.privacy_class == "private"
    assert repo.provider_allow is None
    assert len(repo.id) == 26


def test_ensure_repo_is_idempotent_and_follows_config(db: sqlite3.Connection, repo: Repo) -> None:
    updated = ensure_repo(db, "/code/app", "app2", "client", ["anthropic"])
    assert updated.id == repo.id
    assert updated.created_at == repo.created_at
    assert (updated.name, updated.privacy_class, updated.provider_allow) == (
        "app2",
        "client",
        ["anthropic"],
    )
    assert db.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1


def test_ensure_repo_rejects_unknown_privacy_class(db: sqlite3.Connection) -> None:
    with pytest.raises(StoreError, match="privacy_class"):
        ensure_repo(db, "/x", "x", "secret")  # type: ignore[arg-type]


# Tasks


def test_create_task_round_trip(db: sqlite3.Connection, repo: Repo, task: Task) -> None:
    assert get_task(db, task.id) == task
    assert task.repo_id == repo.id
    assert task.status == "intake"
    assert task.source == "user"
    assert task.finished_at is None
    assert task.risk_signals is None


def test_create_task_with_parent_and_budget(db: sqlite3.Connection, repo: Repo, task: Task) -> None:
    child = create_task(
        db,
        repo.id,
        "sub",
        "sub",
        profile="micro",
        parent_id=task.id,
        category="feature",
        budget_usd=1.5,
    )
    assert (child.parent_id, child.category, child.budget_usd) == (task.id, "feature", 1.5)


def test_create_task_with_unknown_repo_is_a_store_error(db: sqlite3.Connection) -> None:
    with pytest.raises(StoreError, match="cannot create task"):
        create_task(db, "no-such-repo", "x", "x", profile="free")
    assert not db.in_transaction


def test_get_unknown_task_returns_none(db: sqlite3.Connection) -> None:
    assert get_task(db, "nope") is None


@pytest.mark.parametrize(
    ("status", "finished"),
    [("running", False), ("paused", False), ("merged", True), ("failed", True)],
)
def test_set_task_status(db: sqlite3.Connection, task: Task, status: Any, finished: bool) -> None:
    updated = set_task_status(db, task.id, status)
    assert updated.status == status
    assert (updated.finished_at is not None) is finished


def test_set_task_status_rejects_unknown_status_and_task(
    db: sqlite3.Connection, task: Task
) -> None:
    with pytest.raises(StoreError, match="invalid task status"):
        set_task_status(db, task.id, "done")  # type: ignore[arg-type]
    with pytest.raises(StoreError, match="unknown task"):
        set_task_status(db, "nope", "running")


# Attempts


def test_create_attempt_round_trip(db: sqlite3.Connection, attempt: Attempt) -> None:
    assert get_attempt(db, attempt.id) == attempt
    assert attempt.seq == 1
    assert attempt.status == "running"
    assert attempt.route_reason == {"rule": "docs-and-format"}
    assert attempt.mode == "normal"
    assert (attempt.cost_usd, attempt.steps_count, attempt.cost_estimated) == (0.0, 0, False)


def test_attempt_seq_increments_per_task(db: sqlite3.Connection, repo: Repo, task: Task) -> None:
    other = create_task(db, repo.id, "other", "other", profile="free")
    seqs = [create_attempt(db, task.id, **ROUTE).seq for _ in range(3)]
    assert seqs == [1, 2, 3]
    assert create_attempt(db, other.id, **ROUTE).seq == 1


def test_create_attempt_validates_route(db: sqlite3.Connection, task: Task) -> None:
    with pytest.raises(StoreError, match="invalid effort"):
        create_attempt(db, task.id, **{**ROUTE, "effort": "turbo"})
    with pytest.raises(StoreError, match="unknown task"):
        create_attempt(db, "nope", **ROUTE)
    assert not db.in_transaction


@pytest.mark.parametrize(
    ("status", "finished"),
    [("paused", False), ("completed", True), ("timeout", True), ("error", True)],
)
def test_set_attempt_status(
    db: sqlite3.Connection, attempt: Attempt, status: Any, finished: bool
) -> None:
    updated = set_attempt_status(db, attempt.id, status)
    assert updated.status == status
    assert (updated.finished_at is not None) is finished


def test_set_attempt_status_rejects_unknown(db: sqlite3.Connection, attempt: Attempt) -> None:
    with pytest.raises(StoreError, match="invalid attempt status"):
        set_attempt_status(db, attempt.id, "done")  # type: ignore[arg-type]
    with pytest.raises(StoreError, match="unknown attempt"):
        set_attempt_status(db, "nope", "completed")


# Steps


def test_append_step_round_trip(db: sqlite3.Connection, attempt: Attempt) -> None:
    step = append_step(
        db,
        attempt.id,
        "model_call",
        summary="plan",
        payload_ref="t/a/step-0001.json",
        input_tokens=1000,
        output_tokens=200,
        cached_tokens=50,
        price_in_per_m=3.0,
        price_out_per_m=15.0,
        cost_usd=0.006,
    )
    assert step.seq == 1
    assert step.cost_estimated is False
    assert list_steps(db, attempt.id) == [step]


def test_attempt_totals_equal_sum_of_steps(db: sqlite3.Connection, attempt: Attempt) -> None:
    append_step(db, attempt.id, "model_call", input_tokens=100, output_tokens=10, cost_usd=0.5)
    append_step(db, attempt.id, "tool_call", summary="pytest")
    append_step(
        db,
        attempt.id,
        "model_call",
        input_tokens=40,
        output_tokens=4,
        cached_tokens=30,
        cost_usd=0.25,
        cost_estimated=True,
    )

    total = get_attempt(db, attempt.id)
    assert total is not None
    sums = db.execute(
        "SELECT SUM(COALESCE(cost_usd, 0)), SUM(COALESCE(input_tokens, 0)),"
        " SUM(COALESCE(output_tokens, 0)), SUM(COALESCE(cached_tokens, 0)), COUNT(*),"
        " MAX(cost_estimated) FROM steps WHERE attempt_id = ?",
        (attempt.id,),
    ).fetchone()
    assert tuple(sums) == (0.75, 140, 14, 30, 3, 1)
    assert (
        total.cost_usd,
        total.input_tokens,
        total.output_tokens,
        total.cached_tokens,
        total.steps_count,
        total.cost_estimated,
    ) == (0.75, 140, 14, 30, 3, True)
    assert [step.seq for step in list_steps(db, attempt.id)] == [1, 2, 3]


def test_append_step_rejects_unknown_kind_and_attempt(
    db: sqlite3.Connection, attempt: Attempt
) -> None:
    with pytest.raises(StoreError, match="invalid step kind"):
        append_step(db, attempt.id, "thought")  # type: ignore[arg-type]
    with pytest.raises(StoreError, match="unknown attempt"):
        append_step(db, "nope", "message")
    assert list_steps(db, attempt.id) == []
    assert not db.in_transaction


def test_failed_step_write_leaves_no_step_and_no_totals(
    db: sqlite3.Connection, attempt: Attempt
) -> None:
    # Make the totals UPDATE fail after the step INSERT, inside the same transaction.
    db.execute(
        "CREATE TEMP TRIGGER fail_totals BEFORE UPDATE ON attempts"
        " BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )
    with pytest.raises(sqlite3.Error, match="disk full"):
        append_step(db, attempt.id, "model_call", cost_usd=1.0)
    db.execute("DROP TRIGGER fail_totals")

    after = get_attempt(db, attempt.id)
    assert after is not None
    assert (after.cost_usd, after.steps_count) == (0.0, 0)
    assert list_steps(db, attempt.id) == []
    assert not db.in_transaction


# Budget profile fields (BUD-01, RTE-11, CST-10, CST-11)


def test_task_stores_profile_and_deferrable(db: sqlite3.Connection, repo: Repo) -> None:
    task = create_task(db, repo.id, "eval", "eval", profile="free", deferrable=True, source="eval")
    assert (task.profile, task.deferrable) == ("free", True)
    assert get_task(db, task.id) == task


def test_task_defaults_to_not_deferrable(task: Task) -> None:
    assert (task.profile, task.deferrable) == ("micro", False)


@pytest.mark.parametrize("profile", ["unknown", "cheap", "Free", ""])
def test_create_task_rejects_profiles_other_than_the_four(
    db: sqlite3.Connection, repo: Repo, profile: str
) -> None:
    with pytest.raises(StoreError, match="invalid task profile"):
        create_task(db, repo.id, "t", "t", profile=profile)  # type: ignore[arg-type]
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_create_task_requires_a_profile(db: sqlite3.Connection, repo: Repo) -> None:
    with pytest.raises(TypeError):
        create_task(db, repo.id, "t", "t")  # type: ignore[call-arg]


def test_rows_from_before_0002_read_profile_unknown(db: sqlite3.Connection, repo: Repo) -> None:
    db.execute(
        "INSERT INTO tasks (id, repo_id, title, request, status, created_at)"
        " VALUES ('OLD', ?, 't', 't', 'merged', 'x')",
        (repo.id,),
    )
    old = get_task(db, "OLD")
    assert old is not None
    assert (old.profile, old.deferrable) == ("unknown", False)


def test_model_mismatch_flag(db: sqlite3.Connection, attempt: Attempt) -> None:
    assert attempt.model_mismatch is False
    assert set_model_mismatch(db, attempt.id, True).model_mismatch is True
    assert set_model_mismatch(db, attempt.id, False).model_mismatch is False
    with pytest.raises(StoreError, match="unknown attempt"):
        set_model_mismatch(db, "nope", True)


def test_deferred_until(db: sqlite3.Connection, attempt: Attempt) -> None:
    assert attempt.deferred_until is None
    when = "2026-10-08T10:00:00.000Z"
    assert set_deferred_until(db, attempt.id, when).deferred_until == when
    assert set_deferred_until(db, attempt.id, None).deferred_until is None
    with pytest.raises(StoreError, match="unknown attempt"):
        set_deferred_until(db, "nope", when)


@pytest.mark.parametrize(
    "when", ["2026-10-08 10:00:00", "2026-10-08T10:00:00Z", "2026-13-08T10:00:00.000Z", ""]
)
def test_deferred_until_must_be_a_utc_timestamp(
    db: sqlite3.Connection, attempt: Attempt, when: str
) -> None:
    with pytest.raises(StoreError, match="deferred_until must look like"):
        set_deferred_until(db, attempt.id, when)


def test_step_price_fields_round_trip(db: sqlite3.Connection, attempt: Attempt) -> None:
    step = append_step(
        db,
        attempt.id,
        "model_call",
        input_tokens=1000,
        cached_tokens=800,
        output_tokens=100,
        price_in_per_m=0.30,
        price_cache_hit_per_m=0.006,
        price_out_per_m=1.20,
        price_window="offpeak",
        price_multiplier=0.5,
        actual_model="DeepSeek-V4.1-Flash",
        cost_usd=0.0001,
    )
    assert (step.price_window, step.price_multiplier, step.price_cache_hit_per_m) == (
        "offpeak",
        0.5,
        0.006,
    )
    assert step.actual_model == "DeepSeek-V4.1-Flash"
    assert list_steps(db, attempt.id) == [step]


def test_step_defaults_to_no_window_and_multiplier_one(
    db: sqlite3.Connection, attempt: Attempt
) -> None:
    step = append_step(db, attempt.id, "tool_call")
    assert (step.price_window, step.price_multiplier, step.actual_model) == (None, 1.0, None)


def test_totals_still_equal_sum_of_steps_with_price_fields(
    db: sqlite3.Connection, attempt: Attempt
) -> None:
    for window, multiplier, cost in (("peak", 1.0, 0.4), ("offpeak", 0.5, 0.2), ("flat", 1.0, 0.1)):
        append_step(
            db,
            attempt.id,
            "model_call",
            input_tokens=100,
            cached_tokens=40,
            output_tokens=10,
            price_window=window,  # type: ignore[arg-type]
            price_multiplier=multiplier,
            price_cache_hit_per_m=0.006,
            cost_usd=cost,
        )
    total = get_attempt(db, attempt.id)
    assert total is not None
    sums = db.execute(
        "SELECT SUM(cost_usd), SUM(input_tokens), SUM(cached_tokens), SUM(output_tokens), COUNT(*)"
        " FROM steps WHERE attempt_id = ?",
        (attempt.id,),
    ).fetchone()
    assert tuple(sums) == pytest.approx((0.7, 300, 120, 30, 3))
    assert (
        total.cost_usd,
        total.input_tokens,
        total.cached_tokens,
        total.output_tokens,
        total.steps_count,
    ) == pytest.approx((0.7, 300, 120, 30, 3))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"price_window": "night"}, "invalid price window"),
        ({"price_multiplier": 0}, "price_multiplier must be > 0"),
        ({"price_multiplier": -0.5}, "price_multiplier must be > 0"),
        ({"price_multiplier": float("nan")}, "price_multiplier must be > 0"),
    ],
)
def test_bad_step_price_fields_are_rejected(
    db: sqlite3.Connection, attempt: Attempt, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(StoreError, match=message):
        append_step(db, attempt.id, "model_call", **kwargs)
    assert list_steps(db, attempt.id) == []

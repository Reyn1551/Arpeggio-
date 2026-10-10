"""eval matrix (M0.8): per-run solved, paired decision against the udiff runs, refusals."""

import sqlite3
from pathlib import Path

import pytest

from arpeggio_ai.evals.matrix import build_matrix, render_markdown
from arpeggio_ai.store.repositories import (
    add_eval_result,
    append_step,
    create_attempt,
    create_eval_run,
    create_task,
    finish_eval_run,
    register_repo,
    set_attempt_status,
    set_failure_reason,
)

TASKS = ("t1", "t2", "t3", "t4", "t5", "t6")


@pytest.fixture
def repo_id(db: sqlite3.Connection, tmp_path: Path) -> str:
    return register_repo(db, tmp_path).id


def make(
    conn: sqlite3.Connection,
    repo_id: str,
    edit_format: str | None,
    solved: set[str],
    *,
    git_sha: str = "a" * 40,
    config_hash: str = "h",
    status: str = "completed",
    repeats: int = 1,
    errors: frozenset[str] = frozenset(),
    summaries: dict[str, str] | None = None,
    reason: str = "checks_failed",
) -> str:
    run = create_eval_run(
        conn,
        strategy="middle",
        split="tuning",
        git_sha=git_sha,
        config_hash=config_hash,
        profile="micro",
        repeats=repeats,
        planned=len(TASKS),
        estimate_usd=0.0,
    )
    for name in TASKS:
        task = create_task(conn, repo_id, name, "r", profile="micro", source="eval")
        attempt = create_attempt(
            conn,
            task.id,
            adapter="api",
            model="tier1.x",
            effort="low",
            verification="light",
            route_reason={},
        )
        if name not in solved and name not in errors and reason != "checks_failed":
            set_failure_reason(conn, attempt.id, reason)
            set_attempt_status(conn, attempt.id, "error")
        else:
            set_attempt_status(conn, attempt.id, "completed")
        if summaries and name in summaries:
            append_step(conn, attempt.id, "message", summary=summaries[name], cost_usd=0.0)
        result = "solved" if name in solved else ("error" if name in errors else "failed")
        add_eval_result(conn, run.id, name, repeat_index=0, status=result, task_id=task.id)
    summary = None if edit_format is None else {"edit_format": edit_format}
    finish_eval_run(conn, run.id, status, reason=None, summary=summary)  # type: ignore[arg-type]
    return run.id


def baseline(conn: sqlite3.Connection, repo_id: str) -> list[str]:
    return [make(conn, repo_id, "udiff", {"t1"}) for _ in range(3)]


def test_candidate_accepted_on_gain_and_no_loss(db: sqlite3.Connection, repo_id: str) -> None:
    ids = baseline(db, repo_id)
    ids += [make(db, repo_id, "search-replace", {"t1", "t2", "t3", "t4"}) for _ in range(3)]
    matrix = build_matrix(db, ids)
    (decision,) = matrix.decisions
    assert decision.edit_format == "search-replace" and decision.verdict == "accepted"
    assert (decision.baseline_mean_solved, decision.mean_solved, decision.gain) == (1.0, 4.0, 3.0)
    assert decision.lost == {"t1": 0}
    assert [run.solved for run in matrix.groups[0].runs] == [1, 1, 1]
    assert matrix.fields["git_sha"] == "a" * 40


def test_gain_below_two_tasks_is_rejected(db: sqlite3.Connection, repo_id: str) -> None:
    ids = baseline(db, repo_id)
    ids += [make(db, repo_id, "whole-file", {"t1", "t2"}) for _ in range(2)]
    ids.append(make(db, repo_id, "whole-file", {"t1", "t2", "t3", "t4"}))
    (decision,) = build_matrix(db, ids).decisions
    assert decision.verdict == "rejected"
    assert decision.gain == pytest.approx(8 / 3 - 1)
    assert any("below +2 tasks" in reason for reason in decision.reasons)


def test_losing_a_baseline_task_twice_is_rejected(db: sqlite3.Connection, repo_id: str) -> None:
    ids = baseline(db, repo_id)
    ids.append(make(db, repo_id, "search-replace", {"t1", "t2", "t3", "t4", "t5"}))
    ids += [make(db, repo_id, "search-replace", {"t2", "t3", "t4", "t5", "t6"}) for _ in range(2)]
    (decision,) = build_matrix(db, ids).decisions
    assert decision.gain is not None and decision.gain >= 2
    assert decision.verdict == "rejected" and decision.lost == {"t1": 2}
    assert "t1, solved by udiff, is lost in 2 of 3 runs" in decision.reasons


def test_losing_a_baseline_task_once_is_allowed(db: sqlite3.Connection, repo_id: str) -> None:
    ids = baseline(db, repo_id)
    ids += [make(db, repo_id, "search-replace", {"t1", "t2", "t3", "t4"}) for _ in range(2)]
    ids.append(make(db, repo_id, "search-replace", {"t2", "t3", "t4", "t5"}))
    (decision,) = build_matrix(db, ids).decisions
    assert decision.verdict == "accepted" and decision.lost == {"t1": 1}


def test_failed_and_missing_runs_show_as_e_and_block_the_verdict(
    db: sqlite3.Connection, repo_id: str
) -> None:
    ids = baseline(db, repo_id)
    ids.append(make(db, repo_id, "whole-file", {"t1", "t2", "t3"}))
    ids.append(make(db, repo_id, "whole-file", {"t1"}, status="aborted"))
    matrix = build_matrix(db, ids, expect=3)
    whole = matrix.groups[1]
    assert [(run.status, run.solved) for run in whole.runs] == [
        ("completed", 3),
        ("aborted", None),
        ("missing", None),
    ]
    assert whole.runs[1].cells == dict.fromkeys(TASKS, "E")
    assert whole.runs[2].cells == dict.fromkeys(TASKS, "E") and whole.runs[2].run_id is None
    (decision,) = matrix.decisions
    assert decision.verdict == "incomplete"
    assert decision.reasons == ["whole-file: 1 of 3 runs completed"]
    markdown = render_markdown(matrix)
    assert "| whole-file #2 | `" in markdown and "| aborted | E |" in markdown
    assert "| whole-file #3 | `-` | missing | E |" in markdown
    assert "| t2 | 0 | 0 | 0 | 1 | E | E |" in markdown


def test_error_cells_in_a_completed_run(db: sqlite3.Connection, repo_id: str) -> None:
    run = make(db, repo_id, "udiff", {"t1"}, errors=frozenset({"t2"}))
    matrix = build_matrix(db, [run], expect=1)
    assert matrix.groups[0].runs[0].cells["t2"] == "E"
    assert matrix.groups[0].runs[0].solved == 1


@pytest.mark.parametrize(
    ("field", "kwargs"), [("git_sha", {"git_sha": "b" * 40}), ("config_hash", {"config_hash": "z"})]
)
def test_runs_of_different_code_or_config_are_refused(
    db: sqlite3.Connection, repo_id: str, field: str, kwargs: dict[str, str]
) -> None:
    ids = baseline(db, repo_id)
    ids.append(make(db, repo_id, "search-replace", {"t1"}, **kwargs))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=f"runs differ in {field}"):
        build_matrix(db, ids)


def test_runs_with_repeats_are_refused(db: sqlite3.Connection, repo_id: str) -> None:
    run = make(db, repo_id, "udiff", {"t1"}, repeats=3)
    with pytest.raises(ValueError, match="more than one repeat"):
        build_matrix(db, [run])


def test_no_ids_unknown_ids_and_duplicates(db: sqlite3.Connection, repo_id: str) -> None:
    with pytest.raises(ValueError, match="give the eval run ids"):
        build_matrix(db, [])
    with pytest.raises(ValueError, match="unknown eval runs"):
        build_matrix(db, ["nope"])
    run = make(db, repo_id, "udiff", {"t1"})
    with pytest.raises(ValueError, match="given twice"):
        build_matrix(db, [run, run])


def test_runs_before_m08_count_as_udiff(db: sqlite3.Connection, repo_id: str) -> None:
    run = make(db, repo_id, None, {"t1"})
    matrix = build_matrix(db, [run], expect=1)
    assert [g.edit_format for g in matrix.groups] == ["udiff"] and matrix.decisions == []


def test_no_baseline_is_incomplete(db: sqlite3.Connection, repo_id: str) -> None:
    run = make(db, repo_id, "whole-file", {"t1"})
    (decision,) = build_matrix(db, [run], expect=1).decisions
    assert decision.verdict == "incomplete" and decision.reasons == ["no udiff runs in the matrix"]


def test_secondary_counts_near_misses_and_flags(db: sqlite3.Connection, repo_id: str) -> None:
    summaries = {
        "t1": "edits: 1 file (line endings lf: 1), largest shrink 35%",
        "t2": "edits: 1 file (line endings lf: 1), largest shrink 90%",  # not solved
        "t3": "patch_search_not_found: a.py: SEARCH of block 1 matches nothing"
        " (whitespace_near_miss)",
    }
    run = make(
        db, repo_id, "search-replace", {"t1"}, summaries=summaries, reason="patch_search_not_found"
    )
    matrix = build_matrix(db, [run], expect=1)
    only = matrix.groups[0].runs[0]
    assert only.flagged == ["t1 (35%)"]
    assert only.near_misses == 1
    assert only.failures["patch_search_not_found"] == 5
    markdown = render_markdown(matrix)
    assert "| search-replace #1 | 0 | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 | 1 | t1 (35%) |" in markdown


def test_context_runs_are_shown_but_not_used(db: sqlite3.Connection, repo_id: str) -> None:
    old = make(db, repo_id, None, {"t1"}, git_sha="c" * 40)
    ids = baseline(db, repo_id)
    matrix = build_matrix(db, ids, context_ids=[old])
    (context,) = matrix.context
    assert (context.run_id, context.solved, context.task_runs) == (old, 1, 6)
    assert context.solved_tasks == ["t1"] and context.git_sha == "c" * 40
    assert "## Context only (not in the decision)" in render_markdown(matrix)

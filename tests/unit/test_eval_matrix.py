"""eval matrix (M0.8): per-run solved, paired decision against the udiff runs, refusals."""

import sqlite3
from pathlib import Path

import pytest

from arpeggio_ai.evals.matrix import build_matrix, matching_runs, parse_since, render_markdown
from arpeggio_ai.store.repositories import (
    add_eval_result,
    append_step,
    create_attempt,
    create_eval_run,
    create_task,
    finish_eval_run,
    list_eval_runs,
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
    round_index: int | None = None,
    provider: frozenset[str] = frozenset(),
    context_selection: str | None = None,
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
        if name in provider:
            set_attempt_status(conn, attempt.id, "error")  # HTTP 429 after retries
        elif name not in solved and name not in errors and reason != "checks_failed":
            set_failure_reason(conn, attempt.id, reason)
            set_attempt_status(conn, attempt.id, "error")
        else:
            set_attempt_status(conn, attempt.id, "completed")
        if summaries and name in summaries:
            append_step(conn, attempt.id, "message", summary=summaries[name], cost_usd=0.0)
        failed = "error" if name in errors | provider else "failed"
        result = "solved" if name in solved else failed
        add_eval_result(conn, run.id, name, repeat_index=0, status=result, task_id=task.id)
    summary = None if edit_format is None else {"edit_format": edit_format, "round": round_index}
    if summary is not None and context_selection is not None:
        summary["context_selection"] = context_selection
    finish_eval_run(conn, run.id, status, reason=None, summary=summary)  # type: ignore[arg-type]
    return run.id


def started(conn: sqlite3.Connection, run_id: str, at: str) -> None:
    conn.execute("UPDATE eval_runs SET started_at = ? WHERE id = ?", (at, run_id))
    conn.commit()


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
    # Two runs made in the same millisecond have random ULID order: pin their start times.
    started(db, ids[-2], "2026-10-10T00:00:00.000Z")
    started(db, ids[-1], "2026-10-10T00:00:01.000Z")
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
    assert (
        "| search-replace #1 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 0 | 0 | 0 | 1 | t1 (35%) |" in markdown
    )


def test_context_runs_are_shown_but_not_used(db: sqlite3.Connection, repo_id: str) -> None:
    old = make(db, repo_id, None, {"t1"}, git_sha="c" * 40)
    ids = baseline(db, repo_id)
    matrix = build_matrix(db, ids, context_ids=[old])
    (context,) = matrix.context
    assert (context.run_id, context.solved, context.task_runs) == (old, 1, 6)
    assert context.solved_tasks == ["t1"] and context.git_sha == "c" * 40
    assert "## Context only (not in the decision)" in render_markdown(matrix)


# Context config (M0.8 change 3)


def test_context_config_match_and_mismatch_stay_out_of_the_decision(
    db: sqlite3.Connection, repo_id: str
) -> None:
    same = make(db, repo_id, None, {"t1", "t2"}, git_sha="c" * 40)
    other = make(db, repo_id, None, {"t1", "t2", "t3"}, git_sha="d" * 40, config_hash="zz")
    ids = baseline(db, repo_id)
    ids += [make(db, repo_id, "search-replace", {"t1", "t2", "t3", "t4"}) for _ in range(3)]
    plain = build_matrix(db, ids)
    matrix = build_matrix(db, ids, context_ids=[same, other])  # no "runs differ" refusal
    assert [(c.run_id, c.config) for c in matrix.context] == [
        (same, "match"),
        (other, "mismatch"),
    ]
    assert matrix.decisions == plain.decisions
    assert [g.mean_solved for g in matrix.groups] == [1.0, 4.0]
    markdown = render_markdown(matrix)
    assert f"| `{other}` | udiff | `dddddddd` | mismatch | 3/6 |" in markdown
    assert f"| `{same}` | udiff | `cccccccc` | match | 2/6 |" in markdown


# Provider errors (M0.8 change 2)


def test_provider_errors_are_a_column_apart_from_format_labels(
    db: sqlite3.Connection, repo_id: str
) -> None:
    clean = [make(db, repo_id, "udiff", {"t1"}, round_index=n) for n in (1, 2, 3)]
    candidate = [
        make(db, repo_id, "whole-file", {"t1", "t2", "t3", "t4"}, round_index=n) for n in (1, 2)
    ]
    hit = make(
        db,
        repo_id,
        "whole-file",
        {"t1", "t2", "t3", "t4"},
        round_index=3,
        provider=frozenset({"t5", "t6"}),
    )
    matrix = build_matrix(db, [*clean, *candidate, hit])
    run = matrix.groups[1].runs[2]
    assert run.run_id == hit and run.provider_errors == 2
    assert run.cells["t5"] == run.cells["t6"] == "P"
    assert sum(run.failures.values()) == 0  # neither a patch_* label nor checks_failed
    assert run.solved == 4
    (decision,) = matrix.decisions
    assert decision.mean_solved == 4.0 and decision.verdict == "accepted"
    markdown = render_markdown(matrix)
    assert "| whole-file r3 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | -" in markdown
    assert "| t5 | 0 | 0 | 0 | 0 | 0 | P |" in markdown


# Retries (M0.8 change 4)


def test_a_retried_round_fills_its_slot_without_a_fourth_run(
    db: sqlite3.Connection, repo_id: str
) -> None:
    first = make(db, repo_id, "udiff", {"t1"}, round_index=1)
    broken = make(db, repo_id, "udiff", {"t1"}, round_index=2, status="aborted")
    retry = make(db, repo_id, "udiff", {"t1", "t2"}, round_index=2)
    third = make(db, repo_id, "udiff", {"t1"}, round_index=3)
    matrix = build_matrix(db, [first, broken, retry, third])
    (group,) = matrix.groups
    assert [(r.run_id, r.round) for r in group.runs] == [(first, 1), (retry, 2), (third, 3)]
    assert len(group.completed) == 3 and group.mean_solved == pytest.approx(4 / 3)
    assert [(s.run_id, s.status, s.chosen) for s in group.superseded] == [
        (broken, "aborted", retry)
    ]
    markdown = render_markdown(matrix)
    assert "udiff r4" not in markdown and "udiff #4" not in markdown
    assert f"| `{broken}` | udiff | 2 | aborted | 0 | `{retry}` |" in markdown


def test_a_retry_replaces_a_run_with_provider_errors(db: sqlite3.Connection, repo_id: str) -> None:
    hit = make(db, repo_id, "udiff", set(), round_index=1, provider=frozenset({"t1"}))
    retry = make(db, repo_id, "udiff", {"t1"}, round_index=1)
    later = make(db, repo_id, "udiff", {"t1", "t2"}, round_index=1)
    for second, run_id in enumerate((hit, retry, later)):  # not the same millisecond
        started(db, run_id, f"2026-10-10T00:00:0{second}.000Z")
    (group,) = build_matrix(db, [hit, retry, later], expect=1).groups
    assert [r.run_id for r in group.runs] == [retry]  # earliest clean, not the best
    assert {s.run_id for s in group.superseded} == {hit, later}


def test_runs_without_a_round_are_ordered_by_start_then_id(
    db: sqlite3.Connection, repo_id: str
) -> None:
    # Regression (Linux run on fcb7cfd): slots of runs without a round were sorted by run id
    # alone, and ULIDs made in the same millisecond have a random order.
    done = make(db, repo_id, "whole-file", {"t1", "t2"})
    aborted = make(db, repo_id, "whole-file", {"t1"}, status="aborted")
    low, high = sorted([done, aborted])
    started(db, high, "2026-10-10T00:00:00.000Z")  # start order is the reverse of id order
    started(db, low, "2026-10-10T00:00:00.001Z")
    (group,) = build_matrix(db, [done, aborted], expect=2).groups
    assert [r.run_id for r in group.runs] == [high, low]
    started(db, low, "2026-10-10T00:00:00.000Z")  # same millisecond: the id decides
    for ids in ([done, aborted], [aborted, done]):
        (group,) = build_matrix(db, ids, expect=2).groups
        assert [r.run_id for r in group.runs] == [low, high]


def test_slot_choice_does_not_depend_on_the_order_of_ids(
    db: sqlite3.Connection, repo_id: str
) -> None:
    one = make(db, repo_id, "udiff", {"t1"}, round_index=1)
    two = make(db, repo_id, "udiff", {"t1", "t2"}, round_index=1)
    for run_id in (one, two):
        started(db, run_id, "2026-10-10T00:00:00.000Z")
    chosen = set()
    for ids in ([one, two], [two, one]):
        (group,) = build_matrix(db, ids, expect=1).groups
        chosen.add(group.runs[0].run_id)
    assert chosen == {min(one, two)}


@pytest.mark.parametrize("status", ["aborted", "partial", "running"])
def test_a_slot_never_prefers_a_run_that_did_not_complete(
    db: sqlite3.Connection, repo_id: str, status: str
) -> None:
    # The rule in _choose: [clean completed] or [completed] or [latest not completed], each
    # in (started_at, id) order, first item. A completed run always wins.
    earlier = make(db, repo_id, "udiff", {"t1", "t2", "t3"}, round_index=1, status=status)
    hit = make(db, repo_id, "udiff", set(), round_index=1, provider=frozenset({"t1"}))
    later = make(db, repo_id, "udiff", {"t1"}, round_index=1)
    started(db, earlier, "2026-10-10T00:00:00.000Z")
    started(db, hit, "2026-10-10T00:00:01.000Z")
    started(db, later, "2026-10-10T00:00:02.000Z")
    (group,) = build_matrix(db, [earlier, hit, later], expect=1).groups
    assert group.runs[0].run_id == later  # clean completed beats an earlier unfinished run
    (group,) = build_matrix(db, [earlier, hit], expect=1).groups
    assert group.runs[0].run_id == hit  # completed with provider errors still beats unfinished
    assert group.runs[0].status == "completed"


def test_a_round_with_only_failed_runs_stays_e(db: sqlite3.Connection, repo_id: str) -> None:
    ok = make(db, repo_id, "udiff", {"t1"}, round_index=1)
    bad = make(db, repo_id, "udiff", {"t1"}, round_index=2, status="aborted")
    (group,) = build_matrix(db, [ok, bad], expect=2).groups
    assert [(r.round, r.status, r.solved) for r in group.runs] == [
        (1, "completed", 1),
        (2, "aborted", None),
    ]
    assert len(group.completed) == 1


def test_missing_rounds_are_padded_by_number(db: sqlite3.Connection, repo_id: str) -> None:
    only = make(db, repo_id, "udiff", {"t1"}, round_index=2)
    (group,) = build_matrix(db, [only], expect=3).groups
    assert [(r.round, r.status) for r in group.runs] == [
        (1, "missing"),
        (2, "completed"),
        (3, "missing"),
    ]


# Reconciliation filter (M0.8 change 1)


def test_matching_runs_filters_strategy_sha_config_and_time(
    db: sqlite3.Connection, repo_id: str
) -> None:
    keep = make(db, repo_id, "udiff", {"t1"}, round_index=1)
    other_sha = make(db, repo_id, "udiff", {"t1"}, git_sha="b" * 40)
    other_config = make(db, repo_id, "udiff", {"t1"}, config_hash="zz")
    db.execute("UPDATE eval_runs SET strategy = 'junior' WHERE id = ?", (other_sha,))
    db.execute(
        "UPDATE eval_runs SET started_at = '2026-10-09T23:59:59.999Z' WHERE id = ?",
        (other_config,),
    )
    db.execute("UPDATE eval_runs SET started_at = '2026-10-10T00:00:00.000Z' WHERE id = ?", (keep,))
    db.commit()
    runs = list_eval_runs(db)
    since = parse_since("2026-10-10T00:00:00.000Z")
    picked = matching_runs(runs, strategy="middle", git_sha="a" * 40, config_hash="h", since=since)
    assert [r.id for r in picked] == [keep]  # started exactly at --since is kept
    assert {r.id for r in matching_runs(runs, config_hash="h")} == {keep, other_sha}
    assert [r.id for r in matching_runs(runs, since=since)] == [keep, other_sha]
    assert len(matching_runs(runs)) == 3


def test_parse_since_needs_a_timezone() -> None:
    assert parse_since("2026-10-10T03:00:00Z").tzinfo is not None
    assert parse_since("2026-10-10T03:00:00.123+07:00").utcoffset() is not None
    with pytest.raises(ValueError, match="needs a timezone"):
        parse_since("2026-10-10T03:00:00")
    with pytest.raises(ValueError, match="not an ISO-8601 time"):
        parse_since("yesterday")


# Context selection grouping (M0.9)


def test_auto_and_manual_are_separate_groups(db: sqlite3.Connection, repo_id: str) -> None:
    """An auto run must not be averaged together with the manual runs it is compared against."""
    manual = [make(db, repo_id, "udiff", {"t1"}, context_selection="manual") for _ in range(3)]
    auto = [make(db, repo_id, "udiff", {"t1", "t2"}, context_selection="auto") for _ in range(3)]
    matrix = build_matrix(db, [*manual, *auto])
    labels = [group.edit_format for group in matrix.groups]
    assert "udiff" in labels and "udiff+auto" in labels

    (decision,) = matrix.decisions
    assert decision.edit_format == "udiff+auto"
    # The baseline for an auto group is the manual group, so the gain is measured against it.
    assert (decision.baseline_mean_solved, decision.mean_solved, decision.gain) == (1.0, 2.0, 1.0)
    assert decision.verdict == "rejected"  # +1 is under the +2 rule, and no tasks were lost
    assert decision.lost == {"t1": 0}  # t1 is solved in every auto slot, so nothing was lost


def test_manual_label_is_unchanged_so_older_reports_still_read(
    db: sqlite3.Connection, repo_id: str
) -> None:
    """The manual group keeps the plain edit-format label, as every M0.8 report has it."""
    runs = [make(db, repo_id, "whole-file", {"t1"}) for _ in range(3)]
    (group,) = build_matrix(db, runs).groups
    assert group.edit_format == "whole-file"


def test_auto_run_without_the_flag_reads_as_manual(db: sqlite3.Connection, repo_id: str) -> None:
    """A run recorded before M0.9 has no context_selection key and must read as manual."""
    runs = [make(db, repo_id, "udiff", {"t1"}) for _ in range(3)]
    assert all((run.summary or {}).get("context_selection") is None for run in list_eval_runs(db))
    (group,) = build_matrix(db, runs).groups
    assert group.edit_format == "udiff"
    assert all(run.context_selection == "manual" for run in group.runs)


def test_auto_and_manual_runs_side_by_side_do_not_mix(db: sqlite3.Connection, repo_id: str) -> None:
    """Each group holds only its own runs, and each is scored against its own slots."""
    manual = [make(db, repo_id, "udiff", {"t1"}) for _ in range(3)]
    auto = [make(db, repo_id, "udiff", {"t1", "t2"}, context_selection="auto") for _ in range(3)]
    matrix = build_matrix(db, [*manual, *auto])
    groups = {group.edit_format: group for group in matrix.groups}
    assert all(run.context_selection == "manual" for run in groups["udiff"].runs)
    assert all(run.context_selection == "auto" for run in groups["udiff+auto"].runs)
    assert groups["udiff"].mean_solved == 1.0
    assert groups["udiff+auto"].mean_solved == 2.0

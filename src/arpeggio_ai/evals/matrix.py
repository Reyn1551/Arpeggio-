"""``arpeggio eval matrix``: per-run solved counts for the edit-format experiment (M0.8, EVL-05).

``eval report`` pools runs per strategy. The M0.8 decision rule needs every run on its own,
so this reads the given runs one by one:

- Runs are grouped by the ``edit_format`` in their summary (``udiff`` when absent, which
  covers every run before M0.8). ``udiff`` is the baseline.
- All given runs must share strategy, split, profile, ``config_hash`` and ``git_sha``, and
  each must have one repeat. Otherwise the matrix is refused: runs of different code or
  config are not a paired comparison.
- Per task and run a cell: ``1`` solved, ``0`` failed, ``E`` error, paused or not recorded,
  ``S`` skipped. A run that did not complete is ``E`` in every cell, and a group with fewer
  than ``expect`` runs gets ``missing`` runs that are ``E`` too, so a failed run shows up
  instead of disappearing.
- Per run: solved, attempt outcomes (``FAILURE_KINDS``), ``whitespace_near_miss`` (a
  search/replace miss that only trailing whitespace caused; never applied) and the solved
  tasks whose edits removed more than ``SHRINK_FLAG`` of an existing file's lines, for
  manual review.

**Decision rule** (fixed before the runs). A candidate is accepted only if its mean solved
exceeds the mean solved of the ``udiff`` runs of the same matrix by at least
``MIN_GAIN_TASKS``, and no task solved in any ``udiff`` run is unsolved in more than
``MAX_LOST_RUNS`` of the candidate's runs. Either group with fewer than ``expect`` completed
runs makes the verdict ``incomplete``. Context runs (``--context-run``, for example the
M0.7 baseline) are shown, never checked for consistency and never used in the verdict.
"""

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from arpeggio_ai.evals.report import FAILURE_KINDS, select_runs
from arpeggio_ai.orchestrator.patch import EDIT_FORMATS
from arpeggio_ai.store.repositories import EvalRun, list_eval_results

BASELINE = "udiff"
MIN_GAIN_TASKS = 2.0
MAX_LOST_RUNS = 1
SHRINK_FLAG = 0.20
CONSISTENT_FIELDS = ("strategy", "split", "profile", "config_hash", "git_sha")
# The edits step summary written by orchestrator.attempts._record_edits.
EDITS_SHRINK = re.compile(r"^edits: .*, largest shrink (\d+)%$")
NEAR_MISS = "(whitespace_near_miss)"
Cell = Literal["1", "0", "E", "S"]
Verdict = Literal["accepted", "rejected", "incomplete"]


@dataclass(slots=True)
class MatrixRun:
    run_id: str | None  # None for a missing run
    edit_format: str
    status: str  # the eval run's status, or "missing"
    cells: dict[str, Cell]
    solved: int | None  # None unless the run completed
    failures: dict[str, int] = field(default_factory=dict)
    near_misses: int = 0
    flagged: list[str] = field(default_factory=list)  # "task (NN%)"

    @property
    def complete(self) -> bool:
        return self.solved is not None


@dataclass(slots=True)
class Group:
    edit_format: str
    runs: list[MatrixRun]

    @property
    def completed(self) -> list[MatrixRun]:
        return [run for run in self.runs if run.complete]

    @property
    def mean_solved(self) -> float | None:
        done = self.completed
        return None if not done else sum(run.solved or 0 for run in done) / len(done)


@dataclass(slots=True)
class Decision:
    edit_format: str
    verdict: Verdict
    mean_solved: float | None
    baseline_mean_solved: float | None
    gain: float | None
    lost: dict[str, int]  # baseline-solved task -> candidate runs where it is not solved
    reasons: list[str]


@dataclass(slots=True)
class ContextRun:
    run_id: str
    edit_format: str
    git_sha: str
    solved: int
    task_runs: int
    solved_tasks: list[str]


@dataclass(slots=True)
class Matrix:
    expect: int
    tasks: list[str]
    fields: dict[str, str]  # CONSISTENT_FIELDS -> the shared value
    groups: list[Group]
    decisions: list[Decision]
    context: list[ContextRun]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def edit_format_of(run: EvalRun) -> str:
    value = (run.summary or {}).get("edit_format", BASELINE)
    return str(value)


def _check_consistent(runs: Sequence[EvalRun]) -> dict[str, str]:
    shared: dict[str, str] = {}
    for name in CONSISTENT_FIELDS:
        values = sorted({str(getattr(run, name)) for run in runs})
        if len(values) > 1:
            raise ValueError(
                f"runs differ in {name} ({', '.join(values)}); a matrix needs runs of the same"
                f" {', '.join(CONSISTENT_FIELDS)}"
            )
        shared[name] = values[0]
    several = [run.id for run in runs if run.repeats != 1]
    if several:
        raise ValueError(f"runs with more than one repeat: {', '.join(several)}")
    return shared


def _failure_kinds(conn: sqlite3.Connection, task_id: str) -> list[str | None]:
    """Per attempt of a task run: its failure reason, ``checks_failed`` or None (as in
    ``eval report``)."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT CASE WHEN a.failure_reason IS NOT NULL THEN a.failure_reason"
            " WHEN a.status = 'completed' AND EXISTS (SELECT 1 FROM verdicts v"
            " WHERE v.attempt_id = a.id AND NOT v.passed) THEN 'checks_failed' END"
            " FROM attempts a WHERE a.task_id = ?",
            (task_id,),
        )
    ]


def _step_summaries(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT s.summary FROM steps s JOIN attempts a ON a.id = s.attempt_id"
            " WHERE a.task_id = ? AND s.kind = 'message' AND s.summary IS NOT NULL",
            (task_id,),
        )
    ]


def load_run(conn: sqlite3.Connection, run: EvalRun) -> MatrixRun:
    cells: dict[str, Cell] = {}
    failures = dict.fromkeys(FAILURE_KINDS, 0)
    near = 0
    flagged: list[str] = []
    for result in list_eval_results(conn, run.id):
        if result.status == "skipped":
            cells[result.eval_task] = "S"
            continue
        cells[result.eval_task] = "1" if result.solved else ("0" if result.status == "failed" else "E")
        if result.task_id is None:
            continue
        for kind in _failure_kinds(conn, result.task_id):
            if kind in failures:
                failures[kind] += 1
        summaries = _step_summaries(conn, result.task_id)
        near += sum(NEAR_MISS in summary for summary in summaries)
        if result.solved:
            shrink = max(
                (int(m.group(1)) for s in summaries if (m := EDITS_SHRINK.match(s))), default=0
            )
            if shrink > SHRINK_FLAG * 100:
                flagged.append(f"{result.eval_task} ({shrink}%)")
    if run.status != "completed":
        return MatrixRun(run.id, edit_format_of(run), run.status, dict.fromkeys(cells, "E"), None)
    solved = sum(cell == "1" for cell in cells.values())
    return MatrixRun(
        run.id, edit_format_of(run), run.status, cells, solved, failures, near, flagged
    )


def decide(baseline: Group, candidate: Group, expect: int) -> Decision:
    base_mean, mean = baseline.mean_solved, candidate.mean_solved
    gain = None if base_mean is None or mean is None else mean - base_mean
    solved_by_baseline = sorted(
        {task for run in baseline.completed for task, cell in run.cells.items() if cell == "1"}
    )
    lost = {
        task: sum(run.cells.get(task) != "1" for run in candidate.runs)
        for task in solved_by_baseline
    }
    reasons: list[str] = []
    for group in (baseline, candidate):
        if len(group.completed) < expect:
            reasons.append(
                f"{group.edit_format}: {len(group.completed)} of {expect} runs completed"
            )
    if reasons:
        return Decision(candidate.edit_format, "incomplete", mean, base_mean, gain, lost, reasons)
    assert gain is not None
    accepted = True
    if gain < MIN_GAIN_TASKS:
        accepted = False
        reasons.append(f"mean solved gain {gain:+.2f} is below +{MIN_GAIN_TASKS:.0f} tasks")
    for task, count in lost.items():
        if count > MAX_LOST_RUNS:
            accepted = False
            reasons.append(f"{task}, solved by {BASELINE}, is lost in {count} of {expect} runs")
    if accepted:
        reasons.append(f"mean solved gain {gain:+.2f}; no {BASELINE}-solved task lost twice")
    verdict: Verdict = "accepted" if accepted else "rejected"
    return Decision(candidate.edit_format, verdict, mean, base_mean, gain, lost, reasons)


def build_matrix(
    conn: sqlite3.Connection,
    run_ids: Sequence[str],
    *,
    expect: int = 3,
    context_ids: Sequence[str] = (),
) -> Matrix:
    if not run_ids:
        raise ValueError("give the eval run ids of the experiment")
    if expect < 1:
        raise ValueError("--expect must be >= 1")
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("a run id is given twice")
    runs = select_runs(conn, run_ids)
    shared = _check_consistent(runs)
    loaded = [load_run(conn, run) for run in runs]
    order: list[str] = [f for f in EDIT_FORMATS if any(r.edit_format == f for r in loaded)]
    order += sorted({r.edit_format for r in loaded} - set(order))
    tasks = sorted({task for run in loaded for task in run.cells})
    groups: list[Group] = []
    for edit_format in order:
        mine = [run for run in loaded if run.edit_format == edit_format]
        for _ in range(expect - len(mine)):
            mine.append(MatrixRun(None, edit_format, "missing", dict.fromkeys(tasks, "E"), None))
        for run in mine:
            for task in tasks:
                run.cells.setdefault(task, "E")
        groups.append(Group(edit_format, mine))
    baseline = next((g for g in groups if g.edit_format == BASELINE), None)
    decisions: list[Decision] = []
    for group in groups:
        if group.edit_format == BASELINE:
            continue
        if baseline is None:
            decisions.append(
                Decision(
                    group.edit_format, "incomplete", group.mean_solved, None, None, {},
                    [f"no {BASELINE} runs in the matrix"],
                )
            )
            continue
        decisions.append(decide(baseline, group, expect))
    context = []
    for other in select_runs(conn, context_ids) if context_ids else []:
        results = [r for r in list_eval_results(conn, other.id) if r.status != "skipped"]
        solved = sorted(r.eval_task for r in results if r.solved)
        context.append(
            ContextRun(
                other.id, edit_format_of(other), other.git_sha, len(solved), len(results), solved
            )
        )
    return Matrix(expect, tasks, shared, groups, decisions, context)


def _mean(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def render_markdown(matrix: Matrix) -> str:
    lines = ["# Edit-format matrix (M0.8)", ""]
    lines += [f"- {name}: `{value}`" for name, value in matrix.fields.items()]
    lines += [f"- expected runs per format: {matrix.expect}", ""]
    runs = [run for group in matrix.groups for run in group.runs]
    labels = [f"{run.edit_format} #{n}" for g in matrix.groups for n, run in enumerate(g.runs, 1)]
    lines += ["## Solved per run", "", "| run | id | status | solved |", "|---|---|---|---|"]
    for label, run in zip(labels, runs, strict=True):
        solved = "E" if run.solved is None else str(run.solved)
        lines.append(f"| {label} | `{run.run_id or '-'}` | {run.status} | {solved} |")
    lines += ["", "| format | completed runs | mean solved |", "|---|---|---|"]
    for group in matrix.groups:
        lines.append(
            f"| {group.edit_format} | {len(group.completed)} | {_mean(group.mean_solved)} |"
        )
    lines += ["", "## Decision", ""]
    for d in matrix.decisions:
        lines.append(
            f"- **{d.edit_format}: {d.verdict}** (mean {_mean(d.mean_solved)} vs {BASELINE}"
            f" {_mean(d.baseline_mean_solved)}): {'; '.join(d.reasons)}"
        )
    if not matrix.decisions:
        lines.append("- no candidate format in the matrix")
    lines += ["", "## Per task", "", "| task | " + " | ".join(labels) + " |"]
    lines.append("|---|" + "---|" * len(labels))
    for task in matrix.tasks:
        lines.append(f"| {task} | " + " | ".join(run.cells[task] for run in runs) + " |")
    lines += ["", "`1` solved, `0` failed, `E` error or no result, `S` skipped.", ""]
    lines += ["## Attempt outcomes (secondary)", ""]
    header = [*FAILURE_KINDS, "whitespace_near_miss", "flagged solved"]
    lines += ["| run | " + " | ".join(header) + " |", "|---|" + "---|" * len(header)]
    for label, run in zip(labels, runs, strict=True):
        if not run.complete:
            lines.append(f"| {label} | " + " | ".join("E" for _ in header) + " |")
            continue
        counts = [str(run.failures.get(kind, 0)) for kind in FAILURE_KINDS]
        flagged = ", ".join(run.flagged) or "-"
        lines.append(f"| {label} | " + " | ".join([*counts, str(run.near_misses), flagged]) + " |")
    lines += [
        "",
        f"Flagged: a solved task whose edits removed more than {SHRINK_FLAG:.0%} of an existing"
        " file's lines. Review these before accepting a format.",
    ]
    if matrix.context:
        lines += ["", "## Context only (not in the decision)", ""]
        lines += ["| run | format | git_sha | solved | solved tasks |", "|---|---|---|---|---|"]
        for c in matrix.context:
            lines.append(
                f"| `{c.run_id}` | {c.edit_format} | `{c.git_sha[:8]}` |"
                f" {c.solved}/{c.task_runs} | {', '.join(c.solved_tasks) or '-'} |"
            )
    return "\n".join(lines) + "\n"

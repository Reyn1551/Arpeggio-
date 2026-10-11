"""``arpeggio eval matrix``: per-run solved counts for the edit-format experiment (M0.8, EVL-05).

``eval report`` pools runs per strategy. The M0.8 decision rule needs every run on its own,
so this reads the given runs one by one:

- Runs are grouped by the ``edit_format`` in their summary (``udiff`` when absent, which
  covers every run before M0.8). ``udiff`` is the baseline.
- All given runs must share strategy, split, profile, ``config_hash`` and ``git_sha``, and
  each must have one repeat. Otherwise the matrix is refused: runs of different code or
  config are not a paired comparison.
- Runs carry the ``round`` of ``eval run --round``. A (format, round) pair can be retried,
  which adds a run with the same round. Each pair is one slot: the earliest completed run
  without provider errors, else the earliest completed run, else the latest run that did
  not complete, so a completed run always wins. The other runs of the slot are listed as
  superseded. A run without a round is a slot of its own. Runs and slots are ordered by
  start time, then run id, never by the order the ids were given in. ``expect`` counts completed slots per format, and the rounds
  ``1..expect`` missing from a format become ``missing`` slots.
- Per task and slot a cell: ``1`` solved, ``0`` failed, ``P`` a provider error (HTTP 429,
  5xx, a timeout, retries used up: an attempt that ended ``error`` or ``timeout`` without a
  ``failure_reason``), ``E`` another error, a pause or no result, ``S`` skipped. A run that
  did not complete is ``E`` in every cell, so a failed run shows up instead of disappearing.
- Per slot: solved, provider errors, attempt outcomes (``FAILURE_KINDS``),
  ``whitespace_near_miss`` (a search/replace miss that only trailing whitespace caused;
  never applied) and the solved tasks whose edits removed more than ``SHRINK_FLAG`` of an
  existing file's lines, for manual review. Provider errors are counted apart: they are
  neither a format label nor ``checks_failed``.

**Decision rule** (fixed before the runs). A candidate is accepted only if its mean solved
exceeds the mean solved of the ``udiff`` runs of the same matrix by at least
``MIN_GAIN_TASKS``, and no task solved in any ``udiff`` run is unsolved in more than
``MAX_LOST_RUNS`` of the candidate's runs. It reads solved cells only. Either group with
fewer than ``expect`` completed slots makes the verdict ``incomplete``. Context runs
(``--context-run``, for example the M0.7 baseline) are shown with whether their
``config_hash`` matches, never checked for consistency and never used in the verdict.

``matching_runs`` is the filter behind ``eval runs --strategy --git-sha --config-hash
--since``, which the M0.8 script uses to take its run ids from the database.
"""

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from arpeggio_ai.evals.report import FAILURE_KINDS, select_runs
from arpeggio_ai.orchestrator.patch import EDIT_FORMATS
from arpeggio_ai.store.repositories import EvalRun, list_eval_results

BASELINE = "udiff"
MIN_GAIN_TASKS = 2.0
MAX_LOST_RUNS = 1
SHRINK_FLAG = 0.20
CONSISTENT_FIELDS = ("strategy", "split", "profile", "config_hash", "git_sha")
PROVIDER_ERROR_STATUSES = ("error", "timeout")
# The edits step summary written by orchestrator.attempts._record_edits.
EDITS_SHRINK = re.compile(r"^edits: .*, largest shrink (\d+)%$")
NEAR_MISS = "(whitespace_near_miss)"
Cell = Literal["1", "0", "P", "E", "S"]
Verdict = Literal["accepted", "rejected", "incomplete"]


@dataclass(slots=True)
class MatrixRun:
    run_id: str | None  # None for a missing slot
    edit_format: str
    round: int | None
    status: str  # the eval run's status, or "missing"
    started_at: str
    cells: dict[str, Cell]
    solved: int | None  # None unless the run completed
    provider_errors: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    near_misses: int = 0
    flagged: list[str] = field(default_factory=list)  # "task (NN%)"
    context_selection: str = "manual"  # M0.9

    @property
    def complete(self) -> bool:
        return self.solved is not None


@dataclass(slots=True)
class Superseded:
    run_id: str
    edit_format: str
    round: int | None
    status: str
    provider_errors: int
    chosen: str | None  # the run the slot uses instead


@dataclass(slots=True)
class Group:
    edit_format: str
    runs: list[MatrixRun]  # one per slot
    superseded: list[Superseded] = field(default_factory=list)

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
    lost: dict[str, int]  # baseline-solved task -> candidate slots where it is not solved
    reasons: list[str]


@dataclass(slots=True)
class ContextRun:
    run_id: str
    edit_format: str
    git_sha: str
    config_hash: str
    config: Literal["match", "mismatch"]  # against the matrix's config_hash
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
    return str((run.summary or {}).get("edit_format", BASELINE))


def context_selection_of(run: EvalRun) -> str:
    """How the run chose context (M0.9). Runs predating the flag are ``manual``."""
    return str((run.summary or {}).get("context_selection", "manual"))


def round_of(run: EvalRun) -> int | None:
    value = (run.summary or {}).get("round")
    return value if isinstance(value, int) else None


def parse_since(text: str) -> datetime:
    """An ISO-8601 moment with a timezone, such as ``2026-10-10T03:00:00.000Z``."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"--since is not an ISO-8601 time: {text}") from None
    if moment.tzinfo is None:
        raise ValueError(f"--since needs a timezone, for example a trailing Z: {text}")
    return moment


def _started(run: EvalRun) -> datetime:
    return datetime.strptime(run.started_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def matching_runs(
    runs: Sequence[EvalRun],
    *,
    strategy: str | None = None,
    git_sha: str | None = None,
    config_hash: str | None = None,
    since: datetime | None = None,
) -> list[EvalRun]:
    """The runs that match every given filter; ``since`` keeps runs started at or after it."""
    return [
        run
        for run in runs
        if (strategy is None or run.strategy == strategy)
        and (git_sha is None or run.git_sha == git_sha)
        and (config_hash is None or run.config_hash == config_hash)
        and (since is None or _started(run) >= since)
    ]


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


def _attempts(conn: sqlite3.Connection, task_id: str) -> list[tuple[str | None, bool]]:
    """Per attempt of a task run: its outcome kind (failure reason, ``checks_failed`` or
    None, as in ``eval report``) and whether it was a provider error."""
    placeholders = ", ".join("?" for _ in PROVIDER_ERROR_STATUSES)
    return [
        (row[0], bool(row[1]))
        for row in conn.execute(
            "SELECT CASE WHEN a.failure_reason IS NOT NULL THEN a.failure_reason"
            " WHEN a.status = 'completed' AND EXISTS (SELECT 1 FROM verdicts v"
            " WHERE v.attempt_id = a.id AND NOT v.passed) THEN 'checks_failed' END,"
            f" a.failure_reason IS NULL AND a.status IN ({placeholders})"
            " FROM attempts a WHERE a.task_id = ?",
            (*PROVIDER_ERROR_STATUSES, task_id),
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
    provider = near = 0
    flagged: list[str] = []
    for result in list_eval_results(conn, run.id):
        if result.status == "skipped":
            cells[result.eval_task] = "S"
            continue
        attempts = [] if result.task_id is None else _attempts(conn, result.task_id)
        errors = sum(is_provider for _, is_provider in attempts)
        provider += errors
        for kind, _ in attempts:
            if kind in failures:
                failures[kind] += 1
        if result.solved:
            cells[result.eval_task] = "1"
        elif result.status == "failed":
            cells[result.eval_task] = "0"
        else:
            cells[result.eval_task] = "P" if errors else "E"
        if result.task_id is None:
            continue
        summaries = _step_summaries(conn, result.task_id)
        near += sum(NEAR_MISS in summary for summary in summaries)
        if result.solved:
            shrink = max(
                (int(m.group(1)) for s in summaries if (m := EDITS_SHRINK.match(s))), default=0
            )
            if shrink > SHRINK_FLAG * 100:
                flagged.append(f"{result.eval_task} ({shrink}%)")
    common = (run.id, edit_format_of(run), round_of(run), run.status, run.started_at)
    selection = context_selection_of(run)
    if run.status != "completed":
        return MatrixRun(
            *common, dict.fromkeys(cells, "E"), None, provider, context_selection=selection
        )
    solved = sum(cell == "1" for cell in cells.values())
    return MatrixRun(
        *common, cells, solved, provider, failures, near, flagged, context_selection=selection
    )


def _order(run: MatrixRun) -> tuple[str, str]:
    """Start time, then run id: ULIDs made in the same millisecond have a random order, so
    the id only breaks ties, and the result never depends on the order of the given ids."""
    return run.started_at, run.run_id or ""


def _choose(runs: list[MatrixRun]) -> MatrixRun:
    """The run a slot uses: earliest completed without provider errors, else earliest
    completed, else the latest run that did not complete. A completed run always wins."""
    ordered = sorted(runs, key=_order)
    clean = [run for run in ordered if run.complete and run.provider_errors == 0]
    done = [run for run in ordered if run.complete]
    return (clean or done or [ordered[-1]])[0]


def group_key(run: MatrixRun) -> str:
    """The group label: the edit format, and the context selection when it is not ``manual``.

    Keeping ``manual`` implicit means M0.8's matrices and every report written before M0.9 keep
    their labels; an ``auto`` run is a visibly different group rather than a silent mixing.
    """
    if run.context_selection == "manual":
        return run.edit_format
    return f"{run.edit_format}+{run.context_selection}"


def _group(edit_format: str, runs: list[MatrixRun], tasks: list[str], expect: int) -> Group:
    slots: dict[tuple[int, str, str], list[MatrixRun]] = {}
    for run in sorted(runs, key=_order):
        key = (run.round, "", "") if run.round is not None else (10**9, *_order(run))
        slots.setdefault(key, []).append(run)
    rounds = {run.round for run in runs if run.round is not None}
    if rounds or not runs:
        for number in range(1, expect + 1):
            if number not in rounds:
                missing = MatrixRun(None, edit_format, number, "missing", "", {}, None)
                slots[(number, "", "")] = [missing]
    else:
        for extra in range(expect - len(slots)):
            missing = MatrixRun(None, edit_format, None, "missing", "", {}, None)
            slots[(10**9 + 1 + extra, "", "")] = [missing]
    group = Group(edit_format, [])
    for key in sorted(slots):
        chosen = _choose(slots[key])
        for task in tasks:
            chosen.cells.setdefault(task, "E")
        group.runs.append(chosen)
        for other in slots[key]:
            if other is not chosen and other.run_id is not None:
                group.superseded.append(
                    Superseded(
                        other.run_id,
                        edit_format,
                        other.round,
                        other.status,
                        other.provider_errors,
                        chosen.run_id,
                    )
                )
    return group


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
    keys: list[str] = [f for f in EDIT_FORMATS if any(group_key(r) == f for r in loaded)]
    keys += sorted({group_key(r) for r in loaded} - set(keys))
    tasks = sorted({task for run in loaded for task in run.cells})
    groups = [
        _group(key, [run for run in loaded if group_key(run) == key], tasks, expect) for key in keys
    ]
    baseline = next((g for g in groups if g.edit_format == BASELINE), None)
    decisions: list[Decision] = []
    for group in groups:
        if group.edit_format == BASELINE:
            continue
        if baseline is None:
            reasons = [f"no {BASELINE} runs in the matrix"]
            decisions.append(
                Decision(
                    group.edit_format, "incomplete", group.mean_solved, None, None, {}, reasons
                )
            )
            continue
        decisions.append(decide(baseline, group, expect))
    context = []
    for other in select_runs(conn, context_ids) if context_ids else []:
        results = [r for r in list_eval_results(conn, other.id) if r.status != "skipped"]
        solved = sorted(r.eval_task for r in results if r.solved)
        match: Literal["match", "mismatch"] = (
            "match" if other.config_hash == shared["config_hash"] else "mismatch"
        )
        context.append(
            ContextRun(
                other.id,
                edit_format_of(other),
                other.git_sha,
                other.config_hash,
                match,
                len(solved),
                len(results),
                solved,
            )
        )
    return Matrix(expect, tasks, shared, groups, decisions, context)


def _mean(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _labels(matrix: Matrix) -> list[str]:
    labels = []
    for group in matrix.groups:
        for number, run in enumerate(group.runs, start=1):
            suffix = f"r{run.round}" if run.round is not None else f"#{number}"
            labels.append(f"{group.edit_format} {suffix}")
    return labels


def render_markdown(matrix: Matrix) -> str:
    lines = ["# Edit-format matrix (M0.8)", ""]
    lines += [f"- {name}: `{value}`" for name, value in matrix.fields.items()]
    lines += [f"- expected runs per format: {matrix.expect}", ""]
    runs = [run for group in matrix.groups for run in group.runs]
    labels = _labels(matrix)
    lines += ["## Solved per run", "", "| run | id | status | solved | provider errors |"]
    lines.append("|---|---|---|---|---|")
    for label, run in zip(labels, runs, strict=True):
        solved = "E" if run.solved is None else str(run.solved)
        lines.append(
            f"| {label} | `{run.run_id or '-'}` | {run.status} | {solved} | {run.provider_errors} |"
        )
    lines += ["", "| format | completed runs | mean solved |", "|---|---|---|"]
    for group in matrix.groups:
        lines.append(
            f"| {group.edit_format} | {len(group.completed)} | {_mean(group.mean_solved)} |"
        )
    superseded = [s for group in matrix.groups for s in group.superseded]
    if superseded:
        lines += ["", "Superseded runs (a retried format and round):", ""]
        lines += ["| id | format | round | status | provider errors | slot uses |"]
        lines.append("|---|---|---|---|---|---|")
        for s in superseded:
            lines.append(
                f"| `{s.run_id}` | {s.edit_format} | {s.round if s.round is not None else '-'}"
                f" | {s.status} | {s.provider_errors} | `{s.chosen or '-'}` |"
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
    lines += [
        "",
        "`1` solved, `0` failed, `P` provider error, `E` other error or no result, `S` skipped.",
        "",
    ]
    lines += ["## Attempt outcomes (secondary)", ""]
    header = ["provider errors", *FAILURE_KINDS, "whitespace_near_miss", "flagged solved"]
    lines += ["| run | " + " | ".join(header) + " |", "|---|" + "---|" * len(header)]
    for label, run in zip(labels, runs, strict=True):
        if not run.complete:
            lines.append(f"| {label} | " + " | ".join("E" for _ in header) + " |")
            continue
        counts = [str(run.failures.get(kind, 0)) for kind in FAILURE_KINDS]
        flagged = ", ".join(run.flagged) or "-"
        cells = [str(run.provider_errors), *counts, str(run.near_misses), flagged]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Provider errors (HTTP 429, 5xx, timeouts, retries used up) are counted apart from the"
        " format labels and `checks_failed`.",
        f"Flagged: a solved task whose edits removed more than {SHRINK_FLAG:.0%} of an existing"
        " file's lines. Review these before accepting a format.",
    ]
    if matrix.context:
        lines += ["", "## Context only (not in the decision)", ""]
        lines += ["| run | format | git_sha | config | solved | solved tasks |"]
        lines.append("|---|---|---|---|---|---|")
        for c in matrix.context:
            lines.append(
                f"| `{c.run_id}` | {c.edit_format} | `{c.git_sha[:8]}` | {c.config} |"
                f" {c.solved}/{c.task_runs} | {', '.join(c.solved_tasks) or '-'} |"
            )
    return "\n".join(lines) + "\n"

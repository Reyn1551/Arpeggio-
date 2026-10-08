"""``arpeggio eval report``: metrics, bootstrap confidence intervals and breakdowns (EVL-05,
EVL-06).

The unit of a result is one task run (a task under one strategy, one repeat). Sections are
per profile and split: holdout is the headline, tuning is "not for claims". Within a
section every strategy gets:

- task runs, distinct tasks, solved, success rate (solved / task runs),
- total cost (attempts plus verification, failed attempts included) and **cost per solved
  task** (total cost / solved; failed tasks stay in the numerator on purpose),
- mean attempts, escalations, wall-clock and quota wait (0 until M1.11),
- share of cost that was estimated rather than reported, model-mismatch count,
- counterfactual cost and savings against it (CST-05).

**Statistics.** Bootstrap over tasks: tasks are resampled with replacement and a task's
repeats stay together. ``RESAMPLES`` resamples from ``random.Random(seed)``, 95% percentile
intervals for success rate and cost per solved task. A resample with zero solved has no
cost per solved and is dropped; the dropped share is reported, and above
``UNRELIABLE_DROP_SHARE`` the interval is labeled unreliable. Differences against
``middle`` are **paired**: the same resampled tasks for both strategies, restricted to tasks
both ran. With fewer than ``MIN_TASKS`` tasks the section carries a warning.

**Breakdowns.** Success rate and cost per solved per group of tasks: by ``check:`` tags,
by ``difficulty:`` tags (else category) and by ``stack:`` tags (else expected risk).
Groups under ``MIN_GROUP`` tasks are "indicative only".
"""

import random
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from arpeggio_ai.evals.strategies import REFERENCE, STRATEGIES
from arpeggio_ai.store.repositories import EvalRun, list_eval_results, list_eval_runs

RESAMPLES = 10_000
DEFAULT_SEED = 20261008
MIN_TASKS = 10
MIN_GROUP = 5
UNRELIABLE_DROP_SHARE = 0.05
SPLIT_LABELS = {
    "holdout": "headline (holdout)",
    "tuning": "tuning: not for claims",
    "all": "tuning and holdout mixed: not for claims",
}


@dataclass(frozen=True, slots=True)
class TaskRun:
    eval_task: str
    repeat: int
    status: str
    solved: bool
    cost_usd: float
    attempts: int
    escalations: int
    duration_s: float
    quota_wait_s: float
    counterfactual_usd: float
    estimated_cost_usd: float  # part of cost_usd that was estimated, not reported
    mismatches: int
    estimate_usd: float
    tags: tuple[str, ...]
    category: str
    risk: str


@dataclass(frozen=True, slots=True)
class Interval:
    low: float | None
    high: float | None
    dropped_share: float = 0.0  # resamples with no defined value
    unreliable: bool = False


@dataclass(slots=True)
class StrategyMetrics:
    strategy: str
    run_ids: list[str]
    statuses: list[str]
    partial_reasons: list[str]
    task_runs: int
    tasks: int
    solved: int
    success_rate: float | None
    total_cost_usd: float
    cost_per_solved_usd: float | None
    mean_attempts: float | None
    escalations: int
    wall_clock_s: float
    quota_wait_s: float
    estimated_cost_share: float | None
    model_mismatches: int
    counterfactual_usd: float
    savings_vs_counterfactual_usd: float
    estimate_usd: float
    success_ci: Interval
    cost_per_solved_ci: Interval
    skipped: list[dict[str, str]]


@dataclass(frozen=True, slots=True)
class PairedDiff:
    strategy: str
    tasks: int
    success_diff: float | None
    success_ci: Interval
    cost_per_solved_diff_usd: float | None
    cost_per_solved_ci: Interval


@dataclass(frozen=True, slots=True)
class GroupRow:
    group: str
    tasks: int
    indicative_only: bool
    values: dict[str, tuple[float | None, float | None]]  # strategy -> (success, cps)


@dataclass(frozen=True, slots=True)
class Breakdown:
    dimension: str  # check, difficulty, stack
    source: str  # tag prefix used, or the fallback field
    rows: list[GroupRow]


@dataclass(slots=True)
class Section:
    profile: str
    split: str
    label: str
    tasks: int
    warnings: list[str]
    strategies: list[StrategyMetrics]
    paired: list[PairedDiff]
    breakdowns: list[Breakdown]


@dataclass(slots=True)
class Report:
    seed: int
    resamples: int
    run_ids: list[str]
    sections: list[Section] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Loading


def select_runs(conn: sqlite3.Connection, run_ids: Sequence[str] = ()) -> list[EvalRun]:
    """The given runs, or the latest run per (profile, split, strategy)."""
    runs = list_eval_runs(conn)
    if run_ids:
        found = {run.id: run for run in runs}
        missing = [run_id for run_id in run_ids if run_id not in found]
        if missing:
            raise ValueError(f"unknown eval runs: {', '.join(missing)}")
        return [found[run_id] for run_id in run_ids]
    latest: dict[tuple[str, str, str], EvalRun] = {}
    for run in runs:  # oldest first, so later runs replace earlier ones
        latest[(run.profile, run.split, run.strategy)] = run
    return list(latest.values())


def _task_facts(conn: sqlite3.Connection, task_id: str) -> tuple[float, int, str, str]:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN cost_estimated THEN cost_usd ELSE 0 END), 0),"
        " COALESCE(SUM(model_mismatch), 0) FROM attempts WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    task = conn.execute("SELECT category, risk FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return float(row[0]), int(row[1]), str(task[0] or "other"), str(task[1] or "unknown")


def load_task_runs(
    conn: sqlite3.Connection, run: EvalRun
) -> tuple[list[TaskRun], list[dict[str, str]]]:
    rows: list[TaskRun] = []
    skipped: list[dict[str, str]] = []
    for result in list_eval_results(conn, run.id):
        if result.status == "skipped" or result.task_id is None:
            skipped.append({"task": result.eval_task, "reason": result.status_reason or ""})
            continue
        estimated, mismatches, category, risk = _task_facts(conn, result.task_id)
        rows.append(
            TaskRun(
                eval_task=result.eval_task,
                repeat=result.repeat_index,
                status=result.status,
                solved=result.solved,
                cost_usd=result.cost_usd,
                attempts=result.attempts,
                escalations=result.escalations,
                duration_s=result.duration_s,
                quota_wait_s=result.quota_wait_s,
                counterfactual_usd=result.counterfactual_usd or 0.0,
                estimated_cost_usd=estimated,
                mismatches=mismatches,
                estimate_usd=result.estimate_usd or 0.0,
                tags=tuple(result.tags or ()),
                category=category,
                risk=risk,
            )
        )
    return rows, skipped


# Statistics


Stat = Callable[[Sequence[Sequence[TaskRun]]], float | None]


def success_rate(groups: Sequence[Sequence[TaskRun]]) -> float | None:
    runs = sum(len(group) for group in groups)
    return None if runs == 0 else sum(r.solved for g in groups for r in g) / runs


def cost_per_solved(groups: Sequence[Sequence[TaskRun]]) -> float | None:
    solved = sum(r.solved for g in groups for r in g)
    return None if solved == 0 else sum(r.cost_usd for g in groups for r in g) / solved


def _percentiles(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    values = sorted(values)
    last = len(values) - 1
    return values[round(0.025 * last)], values[round(0.975 * last)]


def bootstrap(
    by_task: dict[str, list[TaskRun]], stat: Stat, seed: int, resamples: int = RESAMPLES
) -> Interval:
    """Percentile interval of ``stat`` over task resamples (repeats kept together)."""
    tasks = sorted(by_task)
    if not tasks:
        return Interval(None, None)
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(resamples):
        value = stat([by_task[rng.choice(tasks)] for _ in tasks])
        if value is not None:
            values.append(value)
    dropped = 1 - len(values) / resamples
    low, high = _percentiles(values)
    return Interval(low, high, round(dropped, 4), dropped > UNRELIABLE_DROP_SHARE)


def paired_bootstrap(
    candidate: dict[str, list[TaskRun]],
    reference: dict[str, list[TaskRun]],
    stat: Stat,
    seed: int,
    resamples: int = RESAMPLES,
) -> tuple[float | None, Interval]:
    """Point difference ``stat(candidate) - stat(reference)`` on the tasks both ran, and its
    percentile interval with the same resampled tasks on both sides."""
    tasks = sorted(set(candidate) & set(reference))
    if not tasks:
        return None, Interval(None, None)

    def diff(chosen: Iterable[str]) -> float | None:
        chosen = list(chosen)
        a = stat([candidate[t] for t in chosen])
        b = stat([reference[t] for t in chosen])
        return None if a is None or b is None else a - b

    point = diff(tasks)
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(resamples):
        value = diff(rng.choice(tasks) for _ in tasks)
        if value is not None:
            values.append(value)
    dropped = 1 - len(values) / resamples
    low, high = _percentiles(values)
    return point, Interval(low, high, round(dropped, 4), dropped > UNRELIABLE_DROP_SHARE)


# Building the report


def _by_task(rows: Sequence[TaskRun]) -> dict[str, list[TaskRun]]:
    grouped: dict[str, list[TaskRun]] = defaultdict(list)
    for row in rows:
        grouped[row.eval_task].append(row)
    return dict(grouped)


def _ratio(part: float, whole: float) -> float | None:
    return None if whole == 0 else part / whole


def strategy_metrics(
    strategy: str,
    runs: Sequence[EvalRun],
    rows: Sequence[TaskRun],
    skipped: list[dict[str, str]],
    seed: int,
    resamples: int,
) -> StrategyMetrics:
    by_task = _by_task(rows)
    solved = sum(r.solved for r in rows)
    cost = sum(r.cost_usd for r in rows)
    counterfactual = sum(r.counterfactual_usd for r in rows)
    return StrategyMetrics(
        strategy=strategy,
        run_ids=[run.id for run in runs],
        statuses=[run.status for run in runs],
        partial_reasons=[run.status_reason or "" for run in runs if run.status != "completed"],
        task_runs=len(rows),
        tasks=len(by_task),
        solved=solved,
        success_rate=_ratio(solved, len(rows)),
        total_cost_usd=round(cost, 8),
        cost_per_solved_usd=None if solved == 0 else round(cost / solved, 8),
        mean_attempts=_ratio(sum(r.attempts for r in rows), len(rows)),
        escalations=sum(r.escalations for r in rows),
        wall_clock_s=round(sum(r.duration_s for r in rows), 3),
        quota_wait_s=round(sum(r.quota_wait_s for r in rows), 3),
        estimated_cost_share=_ratio(sum(r.estimated_cost_usd for r in rows), cost),
        model_mismatches=sum(r.mismatches for r in rows),
        counterfactual_usd=round(counterfactual, 8),
        savings_vs_counterfactual_usd=round(counterfactual - cost, 8),
        estimate_usd=round(sum(run.estimate_usd or 0.0 for run in runs), 8),
        success_ci=bootstrap(by_task, success_rate, seed, resamples),
        cost_per_solved_ci=bootstrap(by_task, cost_per_solved, seed, resamples),
        skipped=skipped,
    )


def _groups(
    rows: Sequence[TaskRun], prefix: str, fallback: Callable[[TaskRun], str] | None
) -> tuple[str, dict[str, set[str]]] | None:
    """Group name -> eval tasks, by tag prefix, or by ``fallback`` if no task has one."""
    tagged = any(tag.startswith(prefix) for r in rows for tag in r.tags)
    groups: dict[str, set[str]] = defaultdict(set)
    if tagged:
        for row in rows:
            names = [tag for tag in row.tags if tag.startswith(prefix)] or ["(none)"]
            for name in names:
                groups[name].add(row.eval_task)
        return prefix, dict(groups)
    if fallback is None:
        return None
    for row in rows:
        groups[fallback(row)].add(row.eval_task)
    return "fallback", dict(groups)


DIMENSIONS: tuple[tuple[str, str, Callable[[TaskRun], str] | None, str], ...] = (
    ("check", "check:", None, ""),
    ("difficulty", "difficulty:", lambda r: r.category, "category"),
    ("stack", "stack:", lambda r: r.risk, "expected_risk"),
)


def breakdowns(per_strategy: dict[str, list[TaskRun]]) -> list[Breakdown]:
    every = [row for rows in per_strategy.values() for row in rows]
    result = []
    for dimension, prefix, fallback, fallback_name in DIMENSIONS:
        found = _groups(every, prefix, fallback)
        if found is None:
            continue
        source, groups = found
        table = []
        for name in sorted(groups):
            members = groups[name]
            values = {}
            for strategy, rows in per_strategy.items():
                chosen = [[r for r in rows if r.eval_task == task] for task in sorted(members)]
                chosen = [group for group in chosen if group]
                values[strategy] = (success_rate(chosen), cost_per_solved(chosen))
            table.append(GroupRow(name, len(members), len(members) < MIN_GROUP, values))
        result.append(
            Breakdown(dimension, prefix if source != "fallback" else fallback_name, table)
        )
    return result


def build_report(
    conn: sqlite3.Connection,
    run_ids: Sequence[str] = (),
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = RESAMPLES,
) -> Report:
    runs = select_runs(conn, run_ids)
    report = Report(seed, resamples, [run.id for run in runs])
    keyed: dict[tuple[str, str], dict[str, list[EvalRun]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        keyed[(run.profile, run.split)][run.strategy].append(run)
    order: dict[str, int] = {name: index for index, name in enumerate(STRATEGIES)}
    for (profile, split), by_strategy in sorted(keyed.items()):
        rows_by: dict[str, list[TaskRun]] = {}
        metrics = []
        for strategy in sorted(by_strategy, key=lambda s: (order.get(s, 99), s)):
            rows: list[TaskRun] = []
            skipped: list[dict[str, str]] = []
            for run in by_strategy[strategy]:
                got, skip = load_task_runs(conn, run)
                rows += got
                skipped += skip
            rows_by[strategy] = rows
            metrics.append(strategy_metrics(strategy, by_strategy[strategy], rows, skipped, seed, resamples))
        tasks = len({r.eval_task for rows in rows_by.values() for r in rows})
        warnings = []
        if tasks < MIN_TASKS:
            warnings.append(
                f"too few tasks for reliable conclusions: {tasks} (at least {MIN_TASKS} needed)"
            )
        if any(m.partial_reasons for m in metrics):
            warnings.append("partial run: some planned task runs did not run (see each strategy)")
        paired = []
        if REFERENCE in rows_by:
            reference = _by_task(rows_by[REFERENCE])
            for strategy, rows in rows_by.items():
                if strategy == REFERENCE:
                    continue
                candidate = _by_task(rows)
                s_point, s_ci = paired_bootstrap(candidate, reference, success_rate, seed, resamples)
                c_point, c_ci = paired_bootstrap(
                    candidate, reference, cost_per_solved, seed, resamples
                )
                paired.append(
                    PairedDiff(
                        strategy,
                        len(set(candidate) & set(reference)),
                        s_point,
                        s_ci,
                        c_point,
                        c_ci,
                    )
                )
        else:
            warnings.append(f"no {REFERENCE} run in this section: claims need {REFERENCE}")
        report.sections.append(
            Section(
                profile,
                split,
                SPLIT_LABELS.get(split, split),
                tasks,
                warnings,
                metrics,
                paired,
                breakdowns(rows_by),
            )
        )
    return report


# Markdown


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _signed_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.0f} pp"


def _signed_usd(value: float | None) -> str:
    return "n/a" if value is None else f"{'+' if value >= 0 else '-'}${abs(value):.4f}"


def ci_text(ci: Interval, fmt: Callable[[float | None], str]) -> str:
    if ci.low is None:
        return "n/a"
    text = f"{fmt(ci.low)} to {fmt(ci.high)}"
    if ci.unreliable:
        text += f" (unreliable: {ci.dropped_share:.0%} of resamples had no solved task)"
    return text


def render_markdown(report: Report) -> str:
    lines = ["# Arpeggio baseline report", ""]
    lines.append(
        f"Bootstrap over tasks, {report.resamples} resamples, seed {report.seed}. Claims"
        f" compare against `{REFERENCE}`. Runs: {', '.join(report.run_ids) or 'none'}."
    )
    for section in report.sections:
        lines += ["", f"## Profile `{section.profile}`, split `{section.split}`", ""]
        lines.append(f"**{section.label}.** {section.tasks} tasks.")
        for warning in section.warnings:
            lines += ["", f"> **Warning:** {warning}"]
        lines += [
            "",
            "| Strategy | Runs | Solved | Success | 95% CI | Cost | Cost per solved | 95% CI |"
            " Attempts | Escalations | Wall-clock | Quota wait | Estimated share | Mismatches |"
            " Counterfactual | Savings |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for m in section.strategies:
            name = m.strategy + (" (PARTIAL)" if m.partial_reasons else "")
            lines.append(
                f"| {name} | {m.task_runs} | {m.solved} | {_pct(m.success_rate)} |"
                f" {ci_text(m.success_ci, _pct)} | {_usd(m.total_cost_usd)} |"
                f" {_usd(m.cost_per_solved_usd)} | {ci_text(m.cost_per_solved_ci, _usd)} |"
                f" {'n/a' if m.mean_attempts is None else f'{m.mean_attempts:.2f}'} |"
                f" {m.escalations} | {m.wall_clock_s:.1f}s | {m.quota_wait_s:.1f}s |"
                f" {_pct(m.estimated_cost_share)} | {m.model_mismatches} |"
                f" {_usd(m.counterfactual_usd)} | {_signed_usd(m.savings_vs_counterfactual_usd)} |"
            )
        partial = [(m.strategy, r) for m in section.strategies for r in m.partial_reasons]
        for strategy, reason in partial:
            lines += ["", f"- `{strategy}` is PARTIAL: {reason}"]
        if section.paired:
            lines += [
                "",
                f"### Paired differences against `{REFERENCE}`",
                "",
                "| Strategy | Tasks | Success diff | 95% CI | Cost per solved diff | 95% CI |",
                "|---|---|---|---|---|---|",
            ]
            for p in section.paired:
                lines.append(
                    f"| {p.strategy} | {p.tasks} | {_signed_pct(p.success_diff)} |"
                    f" {ci_text(p.success_ci, _signed_pct)} |"
                    f" {_signed_usd(p.cost_per_solved_diff_usd)} |"
                    f" {ci_text(p.cost_per_solved_ci, _signed_usd)} |"
                )
        for breakdown in section.breakdowns:
            strategies = [m.strategy for m in section.strategies]
            lines += [
                "",
                f"### By {breakdown.dimension} (`{breakdown.source}`)",
                "",
                "| Group | Tasks | "
                + " | ".join(f"{s} success | {s} cost/solved" for s in strategies)
                + " |",
                "|---|---|" + "---|---|" * len(strategies),
            ]
            for row in breakdown.rows:
                cells = []
                for strategy in strategies:
                    success, cps = row.values.get(strategy, (None, None))
                    cells += [_pct(success), _usd(cps)]
                note = " (indicative only)" if row.indicative_only else ""
                lines.append(f"| {row.group}{note} | {row.tasks} | " + " | ".join(cells) + " |")
        skipped = {(s["task"], s["reason"]) for m in section.strategies for s in m.skipped}
        if skipped:
            lines += ["", "### Skipped", ""]
            lines += [f"- `{task}`: {reason}" for task, reason in sorted(skipped)]
    return "\n".join(lines) + "\n"

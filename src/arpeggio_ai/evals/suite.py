"""The eval suite on disk: discovery across the two splits and the latest self-check status.

Layout of an evals directory::

    tasks/<id>.yaml       tuning split
    holdout/<id>.yaml     holdout split (EVL-02)
    fixtures/<id>/...     hidden tests and reference diffs
    .reports/             self-check reports, check-<UTC timestamp>.json
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from arpeggio_ai.evals.task import SPLIT_DIRS, EvalTask, Split, TaskError, load_task

REPORTS_DIR = ".reports"
HOLDOUT_MIN_SHARE = 0.30
SUITE_MIN_TASKS = 20


@dataclass(slots=True)
class TaskEntry:
    id: str
    split: Split
    file: Path
    task: EvalTask | None = None
    problems: list[str] = field(default_factory=list)


def discover(evals_root: Path, variables: Mapping[str, str] | None = None) -> list[TaskEntry]:
    """Every ``*.yaml`` in both splits, loaded and validated, sorted by split then ID.

    An ID present in both splits is a problem on each entry.
    """
    entries: list[TaskEntry] = []
    for split, dirname in SPLIT_DIRS.items():
        for file in sorted((evals_root / dirname).glob("*.yaml")):
            entry = TaskEntry(file.stem, split, file)
            try:
                entry.task = load_task(file, evals_root, variables)
            except TaskError as error:
                entry.problems = error.problems
            entries.append(entry)
    seen: dict[str, list[TaskEntry]] = {}
    for entry in entries:
        seen.setdefault(entry.id, []).append(entry)
    for same in seen.values():
        if len(same) > 1:
            for entry in same:
                entry.task = None
                entry.problems.append(f"id: '{entry.id}' exists in both splits")
    return entries


def select(
    entries: list[TaskEntry], split: str = "all", ids: list[str] | None = None
) -> list[TaskEntry]:
    chosen = [entry for entry in entries if split in ("all", entry.split)]
    if ids:
        chosen = [entry for entry in chosen if entry.id in ids]
    return chosen


_REPORT = re.compile(r"check-[0-9TZ-]+\.json")


def latest_statuses(evals_root: Path) -> dict[str, str]:
    """For each task ID, its status in the newest report that mentions it."""
    statuses: dict[str, str] = {}
    reports = sorted(
        (p for p in (evals_root / REPORTS_DIR).glob("check-*.json") if _REPORT.fullmatch(p.name)),
        reverse=True,
    )
    for report in reports:
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for task in data.get("tasks", []):
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                statuses.setdefault(task["id"], str(task.get("status")))
    return statuses


@dataclass(frozen=True, slots=True)
class SuiteSummary:
    tuning: int
    holdout: int
    warnings: list[str]

    @property
    def total(self) -> int:
        return self.tuning + self.holdout

    @property
    def holdout_share(self) -> float:
        return self.holdout / self.total if self.total else 0.0


def summarize(entries: list[TaskEntry]) -> SuiteSummary:
    """Counts per split and the EVL-01 / EVL-02 warnings (≥ 20 tasks, ≥ 30% holdout)."""
    tuning = sum(1 for entry in entries if entry.split == "tuning")
    holdout = len(entries) - tuning
    summary = SuiteSummary(tuning, holdout, [])
    if summary.total < SUITE_MIN_TASKS:
        summary.warnings.append(
            f"only {summary.total} tasks; EVL-01 asks for at least {SUITE_MIN_TASKS}"
        )
    if summary.holdout_share < HOLDOUT_MIN_SHARE:
        summary.warnings.append(
            f"holdout share {summary.holdout_share:.0%} is below the "
            f"{HOLDOUT_MIN_SHARE:.0%} EVL-02 asks for"
        )
    return summary
